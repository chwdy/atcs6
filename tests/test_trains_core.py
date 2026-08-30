"""
Core train tests - SimulationController, basic placement, position

Run with: pytest tests/test_trains_core.py -v
"""

import logging
import pytest

from track import Tile, TRACK_NS, TRACK_EW, TRACK_NW
from network import RailwayNetwork
from block import Block
from enums import BlockState, Direction, TrainState, HandlePosition, PathStatus
from train import Train, TrainCar
from simulation import SimulationController
from train_controller import TrainController, create_controller_for_train
from path_dispatcher import PathDispatcher
from train_route import Route, RouteMode, RouteTask
from switch import Switch
from tests.segment_test_helpers import make_segment_block_with_tracks


@pytest.fixture
def network():
    return RailwayNetwork()


@pytest.fixture
def straight_ew_track(network):
    for x in range(5):
        track = Tile(x=x, y=0, track_bits=TRACK_EW)
        network.add_track(track)
    return network

class TestSimulationController:
    """Tests for SimulationController class"""

    def test_simulation_creation(self, network):
        simulation = SimulationController(network)
        assert simulation is not None
        assert simulation.network == network
        assert simulation.power_controller is not None
        assert simulation.route_executor is not None
        assert simulation.speed_profile_registry is not None

    def test_simulation_update_without_crash(self, network):
        """Test that simulation.update() can run without crashing (regression test)."""
        simulation = SimulationController(network)

        # Should not crash even with no trains
        simulation.update(0.1)

        # Add a train and update again
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length_cm=20.0)
        train = Train(train_id="TRAIN_01", consist=[car], max_speed=60.0)
        network.add_train(train)

        # Should not crash with train present
        simulation.update(0.1)
        simulation.update(0.1)

    def test_simulation_completes_active_path_at_destination(self, network):
        """Path completion is a Phase-3 step, not a side queue.

        Per design_new.md, `SimulationController` runs the post-physics
        usable-authority stop check and, once the train is stopped at the end
        of its live authority, calls `PathDispatcher.try_complete_active_path(
        ...)`. That handshake transitions the `Path` to COMPLETED and fans out
        to `TrainController.on_path_complete()` and
        `RouteExecutor.handle_path_completion(...)`. There is no separate
        completion-events queue for the runtime to drain.

        We drive a real route-backed journey to its terminal platform and let
        `simulation.update()` run the canonical stop check, then assert the
        COMPLETED transition and the `handle_path_completion` effect (the
        route run finishes, so the train no longer has an assigned route).
        This also covers the historical `handle_path_completion(event_time=...)`
        keyword-signature regression, since that call now fires for real.
        """
        # Single east-west line: STA_A(0,0) -- BLK_A -- STA_B(3,0) -- BLK_C -- STA_C(6,0).
        for x in range(7):
            network.add_track(Tile(x=x, y=0, track_bits=TRACK_EW))
        network.add_station(0, 0, station_id="STA_A", name="A", length_cm=50.0, top_speed_kmh=80.0)
        network.add_station(3, 0, station_id="STA_B", name="B", length_cm=50.0, top_speed_kmh=80.0)
        network.add_station(6, 0, station_id="STA_C", name="C", length_cm=50.0, top_speed_kmh=80.0)
        make_segment_block_with_tracks(network, "BLK_A", positions=[(1, 0), (2, 0)], name="A-B", length_cm=100.0
        )
        make_segment_block_with_tracks(network, "BLK_C", positions=[(4, 0), (5, 0)], name="B-C", length_cm=100.0
        )
        for block in network.blocks.values():
            block.speed_limit_kmh = 80.0

        car = TrainCar(car_id="CAR_1", car_type="LOCOMOTIVE", length_cm=20.0)
        train = Train(train_id="TRAIN_1", consist=[car], max_speed=80.0)
        network.add_train(train)
        placed_span = network.place_train(train, 0, 0, Direction.EAST)

        dispatcher = PathDispatcher(network)
        simulation = SimulationController(network, dispatcher)
        controller = simulation._get_or_create_controller(train)
        # v1: seed the controller-owned live span (add_train does this in
        # production; this test places via network.place_train directly).
        controller.physics_sync.current_span = placed_span
        controller.commands.power_on_cmd()
        controller.commands.set_cab("A")

        # A one-off route to the east terminal. Phase 1 (RouteExecutor) owns
        # creating the authority and departing the train; we only drive ticks.
        # The terminal task is a `stop`, not a `waypoint`: per design_new.md's
        # "Stop-arrival check" (step `8df5`) the `try_complete_active_path(...)`
        # handshake this test exercises completes ONLY `stop` tasks. A terminal
        # `waypoint` is consumed by head entry and its spent authority is
        # released through the revoke-cancel flow (`Path.status -> CANCELLED`),
        # never `COMPLETED` — so a `stop` is what drives this completion path.
        route = Route(route_id="ROUTE_AC", name="A to C", mode=RouteMode.ONE_OFF)
        route.add_task(RouteTask(task_kind="stop", task_location="STA_C"))
        route.assign_train("TRAIN_1")
        route.activate(simulation.simulation_time)
        simulation.route_executor.register_route(route)
        train.assigned_route_id = "ROUTE_AC"

        captured_path_id = None
        moved = False
        completed = False
        for _ in range(1500):
            simulation.update(0.1)
            active = dispatcher.get_active_path("TRAIN_1")
            if active is not None:
                captured_path_id = active.path_id
            if controller.physics_sync.current_speed_kmh > 1.0:
                moved = True
            if captured_path_id is not None:
                row = dispatcher.get_path(captured_path_id)
                if row is not None and row.status == PathStatus.COMPLETED:
                    completed = True
                    break

        assert captured_path_id is not None, (
            "RouteExecutor never created an active path for the train; the "
            "journey could not start."
        )
        assert moved, "Train never departed; the journey did not run."
        assert completed, (
            f"Path {captured_path_id!r} never reached COMPLETED. Final status="
            f"{getattr(dispatcher.get_path(captured_path_id), 'status', None)!r}, "
            f"head_block={controller._get_current_block_id()!r}, "
            f"speed={controller.physics_sync.current_speed_kmh:.2f}, "
            f"state={controller.ops.state.value!r}. Per design_new.md, the "
            f"Phase-3 usable-authority stop check must call "
            f"try_complete_active_path(...) once the train is stopped at the "
            f"end of its authority."
        )

        # Phase-3 completion transitioned the Path to COMPLETED...
        assert dispatcher.get_path(captured_path_id).status == PathStatus.COMPLETED
        # ...so the live projection no longer surfaces it as the active plan.
        assert controller.path_projection.received_path_plan is None
        # ...and handle_path_completion(...) finished the route run, so the
        # train is no longer assigned to ROUTE_AC.
        assert simulation.route_executor.assigned_route_id_for("TRAIN_1") is None
        # ...and completion was delivered through the SimulationController
        # surface, so a `path_completion` NotificationCenter event fired
        # (design_new.md "Path completion notification flow"; gap #6 — the
        # dispatcher must not call
        # RouteExecutor directly and skip the notification).
        completion_notes = [
            n
            for n in simulation.notification_center.peek_recent()
            if getattr(n, "operation", None) == "handle_path_completion"
            and n.has_entity("train", "TRAIN_1")
        ]
        assert completion_notes, (
            "expected a path-completion event for TRAIN_1; the "
            "dispatcher must deliver completion through "
            "SimulationController.handle_path_completion(...)."
        )

    def test_terminal_waypoint_releases_its_path_without_orphaning(self, network):
        """A terminal `waypoint` route releases its authority and finishes.

        This pins the H1/H2 coupling. Per design_new.md's "Stop-arrival check"
        (step `8df5`), `PathDispatcher.try_complete_active_path(...)` completes
        ONLY `stop` tasks — so once completion is gated to stops, a one-off
        route whose final task is a `waypoint` can no longer be released by that
        handshake. A `waypoint` is consumed by head entry, which never releases
        authority, so its live path would sit orphaned forever if nothing else
        released it. The terminal-walk guard (design_new.md step `592a`) is what
        releases it: reaching a terminal walk with a live path still held, Plan
        latches a `revoked` cancel, Action cancels the path
        (`Path.status -> CANCELLED`), and a later Plan runs
        `finish_route_run(...)`. The path must end CANCELLED — never left ACTIVE
        (that is the orphan H1 fixes) and never COMPLETED (completion is a
        stop-only handshake).
        """
        # Same single east-west line as the completion test:
        # STA_A(0,0) -- BLK_A -- STA_B(3,0) -- BLK_C -- STA_C(6,0).
        for x in range(7):
            network.add_track(Tile(x=x, y=0, track_bits=TRACK_EW))
        network.add_station(0, 0, station_id="STA_A", name="A", length_cm=50.0, top_speed_kmh=80.0)
        network.add_station(3, 0, station_id="STA_B", name="B", length_cm=50.0, top_speed_kmh=80.0)
        network.add_station(6, 0, station_id="STA_C", name="C", length_cm=50.0, top_speed_kmh=80.0)
        make_segment_block_with_tracks(network, "BLK_A", positions=[(1, 0), (2, 0)], name="A-B", length_cm=100.0
        )
        make_segment_block_with_tracks(network, "BLK_C", positions=[(4, 0), (5, 0)], name="B-C", length_cm=100.0
        )
        for block in network.blocks.values():
            block.speed_limit_kmh = 80.0

        car = TrainCar(car_id="CAR_1", car_type="LOCOMOTIVE", length_cm=20.0)
        train = Train(train_id="TRAIN_1", consist=[car], max_speed=80.0)
        network.add_train(train)
        placed_span = network.place_train(train, 0, 0, Direction.EAST)

        dispatcher = PathDispatcher(network)
        simulation = SimulationController(network, dispatcher)
        controller = simulation._get_or_create_controller(train)
        controller.physics_sync.current_span = placed_span
        controller.commands.power_on_cmd()
        controller.commands.set_cab("A")

        route = Route(route_id="ROUTE_AC_WP", name="A to C (waypoint)", mode=RouteMode.ONE_OFF)
        route.add_task(RouteTask(task_kind="waypoint", task_location="STA_C"))
        route.assign_train("TRAIN_1")
        route.activate(simulation.simulation_time)
        simulation.route_executor.register_route(route)
        train.assigned_route_id = "ROUTE_AC_WP"

        captured_path_id = None
        moved = False
        finished = False
        for _ in range(1500):
            simulation.update(0.1)
            active = dispatcher.get_active_path("TRAIN_1")
            if active is not None:
                captured_path_id = active.path_id
            if controller.physics_sync.current_speed_kmh > 1.0:
                moved = True
            # The run finishes once the terminal walk is reached with no live
            # path: the assigned route reads None and no active path remains.
            if (
                captured_path_id is not None
                and simulation.route_executor.assigned_route_id_for("TRAIN_1") is None
                and dispatcher.get_active_path("TRAIN_1") is None
            ):
                finished = True
                break

        assert captured_path_id is not None, (
            "RouteExecutor never created an active path for the train."
        )
        assert moved, "Train never departed; the journey did not run."
        assert finished, (
            "The terminal-waypoint run never finished — its path is likely "
            f"orphaned (final status="
            f"{getattr(dispatcher.get_path(captured_path_id), 'status', None)!r}, "
            f"assigned={simulation.route_executor.assigned_route_id_for('TRAIN_1')!r})."
        )

        # The waypoint's authority was RELEASED via the revoke-cancel flow, so
        # the path is CANCELLED — not still ACTIVE (the orphan H1 fixes) and not
        # COMPLETED (completion is stop-only).
        final_status = dispatcher.get_path(captured_path_id).status
        assert final_status == PathStatus.CANCELLED, (
            "expected the terminal waypoint's path to be CANCELLED (released via "
            f"the revoke-cancel flow), got {final_status!r}."
        )
        assert final_status != PathStatus.ACTIVE, "the path is orphaned (still ACTIVE)."
        assert simulation.route_executor.assigned_route_id_for("TRAIN_1") is None
        assert controller.path_projection.received_path_plan is None


class TestTrainPlacement:
    """Tests for proper train placement based on direction and length."""

    def test_place_train_head_at_exit_edge(self, network):
        for x in range(3):
            network.add_track(Tile(x, 0, track_bits=TRACK_EW))
        make_segment_block_with_tracks(network, "BLOCK_1", [(0, 0), (1, 0), (2, 0)], length_cm=150.0
        )
        car = TrainCar("CAR_1", "LOCOMOTIVE", length_cm=30.0)
        train = Train(train_id="T1", consist=[car])
        network.add_train(train)
        span = network.place_train(train, 0, 0, Direction.EAST)
        assert span is not None
        # end_a is the head at placement; it sits at the exit edge of (0,0)
        # having entered from the west edge (facing EAST).
        assert span.end_a.tile == (0, 0)
        assert span.end_a.entry_edge == "W"
        tile_length_cm = network._get_tile_length_cm(0, 0)
        assert span.end_a.distance_cm == tile_length_cm

    def test_place_train_no_track_raises_error(self, network):
        car = TrainCar("CAR_1", "LOCOMOTIVE", length_cm=30.0)
        train = Train(train_id="T1", consist=[car])
        network.add_train(train)
        with pytest.raises(ValueError):
            network.place_train(train, 0, 0, Direction.EAST)



class TestTrainPropertyUpdate:
    """Tests for train property updates."""

    def test_train_name_update(self, network):
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length_cm=20.0)
        train = Train(train_id="TRAIN_01", consist=[car], max_speed=60.0)
        train.name = "Original Name"
        network.add_train(train)
        train.name = "Updated Name"
        assert network.trains["TRAIN_01"].name == "Updated Name"

    def test_train_max_speed_update(self, network):
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length_cm=20.0)
        train = Train(train_id="TRAIN_01", consist=[car], max_speed=60.0)
        network.add_train(train)
        train.max_speed = 120.0
        assert network.trains["TRAIN_01"].max_speed == 120.0

    def test_train_length_update(self, network):
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length_cm=20.0)
        train = Train(train_id="TRAIN_01", consist=[car], max_speed=60.0)
        network.add_train(train)
        assert train.length_cm == 20.0
        train.consist.append(TrainCar(car_id="CAR_02", car_type="FREIGHT", length_cm=30.0))
        assert train.length_cm == 50.0



