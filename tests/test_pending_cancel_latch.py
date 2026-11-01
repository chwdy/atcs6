"""Verify activate_emergency_cmd() sets pending_path_cancel_request as a latch.

Per design_new.md, operator-issued emergency brake (activate_emergency_cmd)
sets eb_active = True, forces handle to EB, clears ato_running, and (when a
live path exists) writes pending_path_cancel_request(EB). The latch survives
across ticks until Action teardown commits it, which only happens when the train is
fully stopped at zero speed.

The doc rule (Latch Table): pending_path_cancel_request is consumed only
when the train is stopped; while the train is still rolling, it survives.
"""

from runtime_payloads import (
    PendingPathCancelRequest,
)
from network import RailwayNetwork
from track import Tile, TRACK_EW
from enums import Direction
from train import Train, TrainCar
from path_dispatcher import PathDispatcher
from simulation import SimulationController
from train_controller import create_controller_for_train
from tests.segment_test_helpers import make_segment_block_with_tracks


def _build_three_block_network() -> RailwayNetwork:
    network = RailwayNetwork()
    for x in range(6):
        network.add_track(Tile(x, 0, track_bits=TRACK_EW))
    make_segment_block_with_tracks(network, "BLK_A", positions=[(0, 0), (1, 0)], name="A", length_cm=200.0
    )
    make_segment_block_with_tracks(network, "BLK_B", positions=[(2, 0), (3, 0)], name="B", length_cm=200.0
    )
    make_segment_block_with_tracks(network, "BLK_C", positions=[(4, 0), (5, 0)], name="C", length_cm=200.0
    )
    return network


def test_activate_emergency_with_path_sets_pending_cancel():
    """Emergency activation while a path is active writes the EB latch."""
    network = _build_three_block_network()
    car = TrainCar(car_id="C1", car_type="LOCOMOTIVE", length_cm=20.0)
    train = Train(train_id="T_EB", consist=[car], max_speed=60.0)
    network.add_train(train)
    span = network.place_train(train, 0, 0, direction=Direction.EAST)

    dispatcher = PathDispatcher(network)
    sim = SimulationController(network, dispatcher)
    controller = sim._get_or_create_controller(train)
    # Seed the controller-owned span so the dispatcher can resolve the train's
    # current block (v1: live position lives on the controller span).
    controller.physics_sync.current_span = span

    # Create a path so received_path_plan is not None.
    result = dispatcher.create_path(
        train_id=train.train_id,
        requested_destination_block_id="BLK_C",
    )
    result.path = dispatcher.get_path(result.path_id) if result.path_id else None
    assert result.success, f"Failed to create path: {result.reason}"
    assert controller.path_projection.received_path_plan is not None

    # Pre-condition: no latch.
    assert controller.route_hooks.phase_latches.path_cancel_request is None
    controller.eb.active = False

    # Activate emergency.
    cmd_result = controller.eb.activate()
    assert cmd_result.success is True
    assert controller.eb.active is True

    # Latch is written with `error.origin == "eb"`.
    latch = controller.route_hooks.phase_latches.path_cancel_request
    assert latch is not None
    assert isinstance(latch, PendingPathCancelRequest)
    assert latch.error.origin == "eb"


def test_activate_emergency_without_path_does_not_set_latch():
    """Emergency activation with no live path: no pending_path_cancel_request."""
    network = _build_three_block_network()
    car = TrainCar(car_id="C1", car_type="LOCOMOTIVE", length_cm=20.0)
    train = Train(train_id="T_EB_NOPATH", consist=[car], max_speed=60.0)
    network.add_train(train)
    network.place_train(train, 0, 0, direction=Direction.EAST)

    dispatcher = PathDispatcher(network)
    controller = create_controller_for_train(
        train_id=train.train_id, network=network, path_dispatcher=dispatcher
    )

    assert controller.path_projection.received_path_plan is None
    cmd_result = controller.eb.activate()
    assert cmd_result.success is True
    assert controller.eb.active is True
    # No live authority -> no cancel latch.
    assert controller.route_hooks.phase_latches.path_cancel_request is None


def test_pending_cancel_latch_survives_while_moving():
    """Latch remains set across simple controller operations until stopped.

    We don't run the full simulation tick chain here (that would require the
    Action-teardown consumer to be fully wired). Instead we directly verify the
    latch is preserved while the train's current_speed > 0 — i.e. the latch
    is durable across reads and not cleared by any incidental call.
    """
    network = _build_three_block_network()
    car = TrainCar(car_id="C1", car_type="LOCOMOTIVE", length_cm=20.0)
    train = Train(train_id="T_MOV", consist=[car], max_speed=60.0)
    network.add_train(train)
    span = network.place_train(train, 0, 0, direction=Direction.EAST)

    dispatcher = PathDispatcher(network)
    sim = SimulationController(network, dispatcher)
    controller = sim._get_or_create_controller(train)
    # Seed the controller-owned span so the dispatcher can resolve the train's
    # current block (v1: live position lives on the controller span).
    controller.physics_sync.current_span = span
    result = dispatcher.create_path(
        train_id=train.train_id,
        requested_destination_block_id="BLK_C",
    )
    result.path = dispatcher.get_path(result.path_id) if result.path_id else None
    assert result.success, f"Failed to create path: {result.reason}"

    # Set the train as moving and activate EB.
    controller.physics_sync.current_speed_kmh = 30.0
    controller.physics_sync.current_speed_kmh = 30.0
    controller.eb.activate()
    assert controller.route_hooks.phase_latches.path_cancel_request is not None

    # Reading the latch repeatedly does not clear it.
    latch_first = controller.route_hooks.phase_latches.path_cancel_request
    latch_second = controller.route_hooks.phase_latches.path_cancel_request
    assert latch_first is latch_second is not None

    # Even after some no-op reads while still moving, the latch stays set.
    _ = controller.path_projection.received_path_plan
    _ = controller.travel_direction
    assert controller.route_hooks.phase_latches.path_cancel_request is not None


def test_activate_emergency_idempotent_rejects_duplicate():
    """When eb_active is already True, activate_emergency_cmd() is rejected."""
    network = _build_three_block_network()
    car = TrainCar(car_id="C1", car_type="LOCOMOTIVE", length_cm=20.0)
    train = Train(train_id="T_DUP", consist=[car], max_speed=60.0)
    network.add_train(train)
    network.place_train(train, 0, 0, direction=Direction.EAST)

    controller = create_controller_for_train(train.train_id, network)
    controller.eb.active = True

    result = controller.eb.activate()
    assert result.success is False


def test_eb_release_waits_for_the_pre_eb_authority_to_clear():
    """A stopped, powered train cannot release EB while its pre-EB reservation
    is still live: the release reads "no live reservation" as one of its four
    conditions (design_new.md "EB workflow state machine", row `ad04`). Once
    the next tick's Action commits the EB cancel and the reservation clears,
    the same release is accepted."""
    network = _build_three_block_network()
    car = TrainCar(car_id="C1", car_type="LOCOMOTIVE", length_cm=20.0)
    train = Train(train_id="T_REL", consist=[car], max_speed=60.0)
    network.add_train(train)
    span = network.place_train(train, 0, 0, direction=Direction.EAST)

    dispatcher = PathDispatcher(network)
    sim = SimulationController(network, dispatcher)
    controller = sim._get_or_create_controller(train)
    controller.physics_sync.current_span = span
    assert controller.commands.power_on_cmd().success
    result = dispatcher.create_path(
        train_id=train.train_id,
        requested_destination_block_id="BLK_C",
    )
    assert result.success, f"Failed to create path: {result.reason}"
    assert controller.is_stopped()

    assert controller.eb.activate().success
    assert controller.path_projection.received_path_plan is not None

    refused = controller.eb.release()
    assert refused.success is False
    assert controller.eb.active is True, "a refused release changes nothing"
    assert controller.route_hooks.phase_latches.path_cancel_request is not None

    sim.update(0.1)
    assert controller.path_projection.received_path_plan is None
    assert dispatcher.get_active_path(train.train_id) is None

    accepted = controller.eb.release()
    assert accepted.success is True, accepted.message
    assert controller.eb.active is False


# =============================================================================
# Inclusive-latch writers (per design_new.md "Latch Table")
# =============================================================================


def test_set_pending_route_abort_sets_abort_latch_without_path():
    """_set_pending_route_abort writes pending_route_abort = True even with no live path."""
    network = _build_three_block_network()
    car = TrainCar(car_id="C1", car_type="LOCOMOTIVE", length_cm=20.0)
    train = Train(train_id="T_ABORT", consist=[car], max_speed=60.0)
    network.add_train(train)
    network.place_train(train, 0, 0, direction=Direction.EAST)

    controller = create_controller_for_train(train.train_id, network)
    assert controller.path_projection.received_path_plan is None
    assert controller.route_hooks.phase_latches.route_abort is False

    controller.route_hooks.phase_latches.set_route_abort(
        "operator stop",
        has_active_path=controller.path_projection.received_path_plan is not None,
    )

    # Per design_new.md the abort latch is bool; the diagnostic reason flows
    # only via the inclusive path_cancellation notification, not via a stored
    # field on this latch.
    assert controller.route_hooks.phase_latches.route_abort is True
    # No live path -> no inclusive cancel latch.
    assert controller.route_hooks.phase_latches.path_cancel_request is None


class _FakeResourceEntry:
    def __init__(self, resource_id):
        self.resource_id = resource_id
        self.path_tiles = []


class _FakePath:
    """Tiny stand-in for a dispatcher Path with the fields the controller reads.

    `reservation_end_block_id` is derived from `resource_sequence[-1]`, not stored,
    so the fake supplies the sequence the projection actually reads.
    """

    def __init__(self, path_id="PATH_X", reservation_end_block_id="BLK_C"):
        self.path_id = path_id
        self.resource_sequence = [_FakeResourceEntry(reservation_end_block_id)]


class _FakePathDispatcher:
    """Just enough of PathDispatcher for the controller to resolve an active
    path through `received_path_plan` without exercising create_path."""

    def __init__(self, path):
        self._path = path

    def get_active_path(self, train_id):
        return self._path


def test_set_pending_route_abort_inclusive_cancel_when_path_exists():
    """_set_pending_route_abort writes pending_route_abort = True + inclusive
    pending_path_cancel_request(error.origin="operator_cancelled") when a live
    path exists."""
    network = _build_three_block_network()
    car = TrainCar(car_id="C1", car_type="LOCOMOTIVE", length_cm=20.0)
    train = Train(train_id="T_ABORT_PATH", consist=[car], max_speed=60.0)
    network.add_train(train)
    network.place_train(train, 0, 0, direction=Direction.EAST)

    fake_path = _FakePath(path_id="PATH_ABORT_001")
    dispatcher = _FakePathDispatcher(fake_path)
    controller = create_controller_for_train(
        train_id=train.train_id, network=network, path_dispatcher=dispatcher
    )

    # The projection is the narrow received_path_plan schema, not the raw
    # dispatcher Path — the controller never sees the dispatcher-only fields.
    plan = controller.path_projection.received_path_plan
    assert plan is not fake_path
    assert plan.path_id == "PATH_ABORT_001"
    assert plan.get_reservation_end_block_id() == "BLK_C"
    assert not hasattr(plan, "requested_destination_block_id")
    assert not hasattr(plan, "status")
    assert controller.path_projection.assigned_path_id == "PATH_ABORT_001"

    controller.route_hooks.phase_latches.set_route_abort(
        "operator stop",
        has_active_path=controller.path_projection.received_path_plan is not None,
    )
    assert controller.route_hooks.phase_latches.route_abort is True

    cancel = controller.route_hooks.phase_latches.path_cancel_request
    assert cancel is not None
    assert cancel.error.origin == "operator_cancelled"
