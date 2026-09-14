"""`ato_status` read-model projection (design_new.md "ATO status").

`ato_status` is not a controller field: `RouteExecutor.ato_status_for(train_id)`
assembles it from the controller's derived `train_status` / `commands.engaged` /
stopped-predicate plus the train's route-run state (whether a RUNNING run exists
and whether its current walk task is a started, unfinished `dwell` row). The
composite builder exposes it on the controller payload.
"""

from enums import AtoStatus
from network import RailwayNetwork
from simulation import SimulationController, build_network_payload
from train import Train, TrainCar
from train_route import Route, RouteMode, RouteRunStatus, RouteTask, RouteTaskRun
from route_executor import RouteExecutionState


def _sim_with_train(train_id="T1"):
    net = RailwayNetwork()
    sim = SimulationController(net)
    car = TrainCar(car_id="C", car_type="LOCOMOTIVE", length_cm=20.0)
    train = Train(train_id=train_id, consist=[car], max_speed=60.0)
    net.add_train(train)
    controller = sim._get_or_create_controller(train)
    return net, sim, controller


def test_ato_status_off_when_not_in_ato_mode():
    net, sim, controller = _sim_with_train()
    # Unpowered -> OFF.
    assert sim.route_executor.ato_status_for("T1") == AtoStatus.OFF.value
    # Powered but manual -> still OFF (ato_status is ATO's own status).
    controller.powered = True
    controller.commands.control_mode = "manual"
    assert sim.route_executor.ato_status_for("T1") == AtoStatus.OFF.value


def test_ato_status_ready_when_ato_but_not_engaged_or_no_run():
    net, sim, controller = _sim_with_train()
    controller.powered = True
    controller.commands.control_mode = "ato"
    # ATO, not engaged, no run -> READY.
    controller.commands.engaged = False
    assert sim.route_executor.ato_status_for("T1") == AtoStatus.READY.value
    # Engaged but still no RUNNING run to pursue -> READY.
    controller.commands.engaged = True
    assert sim.route_executor.ato_status_for("T1") == AtoStatus.READY.value


def _running_run(sim, train_id="T1"):
    run = RouteExecutionState(
        train_id=train_id, route_id="R1", started_at=0.0, run_number=1
    )
    run.current_route_status = RouteRunStatus.RUNNING
    sim.route_executor.execution_states[train_id] = run
    return run


def test_ato_status_running_and_held_track_motion():
    net, sim, controller = _sim_with_train()
    controller.powered = True
    controller.commands.control_mode = "ato"
    controller.commands.engaged = True
    _running_run(sim)

    # Engaged, RUNNING run, moving -> RUNNING.
    controller.physics_sync.current_speed_kmh = 30.0
    assert sim.route_executor.ato_status_for("T1") == AtoStatus.RUNNING.value

    # Engaged, RUNNING run, stopped, not at a station -> HELD.
    controller.physics_sync.current_speed_kmh = 0.0
    assert sim.route_executor.ato_status_for("T1") == AtoStatus.HELD.value


def test_ato_status_dwelling_when_current_task_is_a_started_dwell():
    net, sim, controller = _sim_with_train()
    controller.powered = True
    controller.commands.control_mode = "ato"
    controller.commands.engaged = True
    controller.physics_sync.current_speed_kmh = 0.0

    # A route whose first (and current) task is a dwell.
    route = Route(route_id="R1", name="dwell route", mode=RouteMode.ONE_OFF)
    route.add_task(RouteTask(task_kind="dwell", task_location="STN", task_time=30.0))
    sim.route_executor.register_route(route)

    run = _running_run(sim)
    # In-flight dwell row: started, not finished.
    run.task_runs = [
        RouteTaskRun(task_index=0, walk_direction="forward", started_at=1.0)
    ]

    assert sim.route_executor._run_is_at_started_dwell(run) is True
    assert sim.route_executor.ato_status_for("T1") == AtoStatus.DWELLING.value

    # Before the dwell timer starts (started_at is None) it is not "at station".
    run.task_runs[0].started_at = None
    assert sim.route_executor.ato_status_for("T1") == AtoStatus.HELD.value


def test_composite_payload_exposes_ato_status():
    net, sim, controller = _sim_with_train("TR")
    controller.powered = True
    controller.commands.control_mode = "ato"
    controller.commands.engaged = True
    controller.physics_sync.current_speed_kmh = 25.0
    _running_run(sim, "TR")

    payload = build_network_payload(net, sim)
    assert payload["controllers_by_train_id"]["TR"]["ato_status"] == AtoStatus.RUNNING.value
