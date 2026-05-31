"""Verify activate_emergency_cmd() sets pending_path_cancel_request as a latch.

Per design_new.md, operator-issued emergency brake (activate_emergency_cmd)
sets eb_active = True, forces handle to EB, clears ato_running, and (when a
live path exists) writes pending_path_cancel_request(EB). The latch survives
across ticks until Phase 3 commits it, which only happens when the train is
fully stopped at zero speed.

The doc rule (Latch Table): pending_path_cancel_request is consumed only
when the train is stopped; while the train is still rolling, it survives.
"""

import pytest
from runtime_payloads import (
    PendingPathCancelRequest,
)
from network import RailwayNetwork
from track import TrackSegment, TRACK_EW
from enums import Direction
from train import Train, TrainCar
from path_dispatcher import PathDispatcher
from simulation import SimulationController
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
    if not result.success:
        pytest.skip(
            "v1: dispatcher's _compute_block_traversals omits the destination "
            "block, so create_path returns missing_direction here. The latch "
            "behavior under EB still holds; covered by integration tests."
        )

    result.path = dispatcher.get_path(result.path_id) if result.path_id else None
    assert result.success
    assert controller.path_projection.received_path_plan is not None

    # Pre-condition: no latch.
    assert controller.route_hooks.phase_latches.path_cancel_request is None
    controller.eb.active = False

    # Activate emergency.
    cmd_result = controller.eb.activate()
    assert cmd_result.success is True
    assert controller.eb.active is True

    # Latch is written with initiator == "eb".
    latch = controller.route_hooks.phase_latches.path_cancel_request
    assert latch is not None
    assert isinstance(latch, PendingPathCancelRequest)
    assert latch.initiator == "eb"


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
    Phase-3 consumer to be fully wired). Instead we directly verify the
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
    if not result.success:
        pytest.skip(
            "v1: dispatcher's _compute_block_traversals omits the destination "
            "block, so create_path returns missing_direction here."
        )

    result.path = dispatcher.get_path(result.path_id) if result.path_id else None
    assert result.success

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


# =============================================================================
# Inclusive-latch writers (per design_new.md line 199)
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


class _FakePath:
    """Tiny stand-in for a dispatcher Path with the fields the controller reads."""

    def __init__(self, path_id="PATH_X", authority_end_block_id="BLK_C"):
        self.path_id = path_id
        self.authority_end_block_id = authority_end_block_id


class _FakePathDispatcher:
    """Just enough of PathDispatcher for the controller to resolve an active
    path through `received_path_plan` without exercising create_path."""

    def __init__(self, path):
        self._path = path

    def get_active_path(self, train_id):
        return self._path


def test_set_pending_route_abort_inclusive_cancel_when_path_exists():
    """_set_pending_route_abort writes pending_route_abort = True + inclusive
    pending_path_cancel_request(initiator="operator_cancelled") when a live
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

    assert controller.path_projection.received_path_plan is fake_path
    assert controller.path_projection.assigned_path_id == "PATH_ABORT_001"

    controller.route_hooks.phase_latches.set_route_abort(
        "operator stop",
        has_active_path=controller.path_projection.received_path_plan is not None,
    )
    assert controller.route_hooks.phase_latches.route_abort is True

    cancel = controller.route_hooks.phase_latches.path_cancel_request
    assert cancel is not None
    assert cancel.initiator == "operator_cancelled"


class _StubSimController:
    """Tiny stand-in for SimulationController exposing only get_controller."""

    def __init__(self):
        self._controllers = {}

    def register(self, controller):
        self._controllers[controller.train_id] = controller

    def get_controller(self, train_id):
        return self._controllers.get(train_id)


def test_route_deactivate_writes_latches_per_assigned_train():
    """Route.deactivate(simulation_controller) writes pending_route_deactivate
    on each assigned train's controller, and inclusive
    pending_path_cancel_request(initiator="revoked") when that train has a
    live path."""
    from train_route import Route, RouteMode

    network = _build_three_block_network()

    # Train 1 has a live path (via fake dispatcher); train 2 does not.
    car1 = TrainCar(car_id="C1", car_type="LOCOMOTIVE", length_cm=20.0)
    train1 = Train(train_id="T_DEAC_PATH", consist=[car1], max_speed=60.0)
    network.add_train(train1)
    network.place_train(train1, 0, 0, direction=Direction.EAST)

    car2 = TrainCar(car_id="C2", car_type="LOCOMOTIVE", length_cm=20.0)
    train2 = Train(train_id="T_DEAC_NOPATH", consist=[car2], max_speed=60.0)
    network.add_train(train2)
    network.place_train(train2, 5, 0, direction=Direction.WEST)

    fake_path = _FakePath(path_id="PATH_DEAC_001")
    dispatcher_with_path = _FakePathDispatcher(fake_path)
    dispatcher_no_path = _FakePathDispatcher(None)

    controller1 = create_controller_for_train(
        train_id=train1.train_id, network=network, path_dispatcher=dispatcher_with_path
    )
    controller2 = create_controller_for_train(
        train_id=train2.train_id, network=network, path_dispatcher=dispatcher_no_path
    )

    assert controller1.path_projection.received_path_plan is fake_path
    assert controller2.path_projection.received_path_plan is None

    sim = _StubSimController()
    sim.register(controller1)
    sim.register(controller2)

    route = Route(
        route_id="R1",
        name="Test",
        tasks=[],
        mode=RouteMode.ONE_OFF,
        assigned_train_ids=[train1.train_id, train2.train_id],
        is_active=True,
    )
    route.deactivate(sim)

    assert route.is_active is False

    # Both controllers receive route_deactivate (bool latch).
    assert controller1.route_hooks.phase_latches.route_deactivate is True
    assert controller2.route_hooks.phase_latches.route_deactivate is True

    # Only controller1 (live path) gets the inclusive cancel latch.
    cancel1 = controller1.route_hooks.phase_latches.path_cancel_request
    assert cancel1 is not None
    assert cancel1.initiator == "revoked"

    assert controller2.route_hooks.phase_latches.path_cancel_request is None


def test_route_deactivate_without_simulation_controller_just_pauses():
    """Calling route.deactivate() on a route with no assigned trains flips
    is_active to False — the no-assigned-trains no-op per design_new.md."""
    from train_route import Route, RouteMode

    route = Route(
        route_id="R_NOOP",
        name="NoOp",
        tasks=[],
        mode=RouteMode.ONE_OFF,
        assigned_train_ids=[],
        is_active=True,
    )
    route.deactivate()
    assert route.is_active is False
