"""ATO go needs a route to drive (design_new.md "ATO go").

Like a real ATO start button, Go is offered only once the train has a trip to
drive: it is accepted when `train_status == ATO` and the train has a route
assigned (`RouteExecutor.assigned_route_id_for(train_id)` is not None). With no
route the engage bit would drive nothing, and the next route assignment clears
it anyway, so a Go pressed before the route did nothing at all.
"""

from enums import Direction
from path_dispatcher import PathDispatcher
from simulation import SimulationController
from train import Train, TrainCar
from train_route import Route, RouteMode, RouteTask

from tests.test_block_occupancy_cap import build_line


def _ato_train(powered: bool = True):
    network = build_line(6)
    dispatcher = PathDispatcher(network)
    sim = SimulationController(network, dispatcher)
    train = Train(
        train_id="T",
        consist=[TrainCar(car_id="c1", car_type="LOCOMOTIVE", length_cm=40.0)],
        max_speed=80.0,
    )
    network.add_train(train)
    controller = sim._get_or_create_controller(train)
    controller.physics_sync.current_span = network.place_train(
        train, 0, 0, Direction.EAST
    )
    sim._get_or_create_physics(train)
    network.block_manager.occupy_block("BLK_0", "T")
    if powered:
        assert controller.commands.set_mode("ato").success
    return sim, controller


def _assign_stop_route(sim):
    route = Route(
        route_id="R",
        name="R",
        mode=RouteMode.ONE_OFF,
        tasks=[
            RouteTask(
                task_kind="stop",
                task_location={"block_id": "BLK_4"},
                task_direction="either",
            )
        ],
    )
    sim.route_executor.register_route(route)
    sim.route_executor.request_route_assignment("T", "R")


def test_go_with_no_route_is_refused_and_engages_nothing():
    sim, controller = _ato_train()

    result = controller.commands.ato_go()

    assert result.success is False
    assert "route" in result.message
    assert controller.commands.engaged is False


def test_go_is_accepted_once_a_route_is_assigned_before_its_run_exists():
    sim, controller = _ato_train()
    _assign_stop_route(sim)
    # Plan has not allocated the run yet; the assignment alone is the trip.
    assert sim.route_executor.execution_state_for("T") is None

    assert controller.commands.ato_go().success
    assert controller.commands.engaged is True

    sim.update(0.1)
    assert sim.path_dispatcher.get_active_path("T") is not None


def test_a_queued_power_up_ato_go_batch_is_accepted_with_a_route():
    """The demo start-up batch: from OFF, `set_mode("ato")` (slot 2) then
    `ato_go` (slot 3) in one drain, for a train already on a route."""
    sim, controller = _ato_train(powered=False)
    _assign_stop_route(sim)
    controller.commands.set_mode("ato")
    controller.commands.ato_go()

    sim.update(0.1)

    assert controller.commands.engaged is True


def test_the_rest_endpoint_reports_why_go_was_refused():
    import threading
    from datetime import datetime

    from app_factory import create_app
    from app_state import AppState
    from persistence import PersistenceManager

    sim, controller = _ato_train()
    state = AppState(
        network=sim.network,
        path_dispatcher=sim.path_dispatcher,
        sim_controller=sim,
        persistence_manager=PersistenceManager(storage_dir="layouts"),
        simulation_lock=threading.Lock(),
        simulation_running=False,
        simulation_speed=1.0,
        last_update_time=datetime.now(),
    )
    client = create_app(state).test_client()

    refused = client.post("/api/trains/T/ato-go")
    assert refused.status_code == 400
    assert "route" in refused.get_json()["error"]

    _assign_stop_route(sim)
    accepted = client.post("/api/trains/T/ato-go")
    assert accepted.status_code == 200
    assert controller.commands.engaged is True
