"""`route_runs_by_train_id` composite sibling payload (design_new.md:2309).

Per the canonical "Data Flow To UI" contract, any composite network/train
snapshot must expose a 4th sibling payload group `route_runs_by_train_id`
mapping `train_id -> {route_id, run_number, state, display_state, last_error}`
for the train's currently-`RUNNING` `RouteRun` (None when the train has no
RUNNING run), produced by `RouteExecutor`. The pre-existing
`assigned_routes_by_train_id` map (which the frontend reads) is kept alongside
it.
"""

from network import RailwayNetwork
from route_executor import RouteExecutor, RouteExecutionState
from train_route import RouteRunStatus
from simulation import SimulationController, build_network_payload


def test_route_run_summary_for_running_deviated_finished_missing():
    """`RouteExecutor.route_run_summary_for` returns the 5 doc fields for a
    RUNNING run, reflects `display_state` (DEVIATED via path_cancelled), and is
    None for non-RUNNING or absent runs."""
    ex = RouteExecutor(RailwayNetwork())

    run = RouteExecutionState(
        train_id="T1", route_id="R1", started_at=0.0, run_number=7
    )
    run.current_route_status = RouteRunStatus.RUNNING
    ex.execution_states["T1"] = run

    summary = ex.route_run_summary_for("T1")
    assert summary == {
        "route_id": "R1",
        "run_number": 7,
        "state": "RUNNING",
        "display_state": "RUNNING",
        "last_error": None,
    }

    # display_state projects DEVIATED while path_cancelled, but state stays
    # RUNNING (deviation is not a stored state).
    run.path_cancelled = True
    deviated = ex.route_run_summary_for("T1")
    assert deviated["display_state"] == "DEVIATED"
    assert deviated["state"] == "RUNNING"
    run.path_cancelled = False

    # A terminal (FINISHED) run is not reported as a RUNNING summary.
    run.current_route_status = RouteRunStatus.FINISHED
    assert ex.route_run_summary_for("T1") is None

    # Train with no run at all.
    assert ex.route_run_summary_for("GHOST") is None


def test_build_network_payload_emits_route_runs_sibling():
    """The composite builder emits `route_runs_by_train_id` covering every
    train, with the RUNNING summary populated, while keeping
    `assigned_routes_by_train_id`."""
    net = RailwayNetwork()
    sim = SimulationController(net)

    # Inject a RUNNING run for a train id (no placement needed; the builder
    # iterates `network.trains`, so register a bare Train).
    from train import Train, TrainCar

    car = TrainCar(car_id="C", car_type="LOCOMOTIVE", length_cm=20.0)
    train = Train(train_id="TR", consist=[car], max_speed=60.0)
    net.add_train(train)

    run = RouteExecutionState(
        train_id="TR", route_id="ROUTE_X", started_at=0.0, run_number=3
    )
    run.current_route_status = RouteRunStatus.RUNNING
    sim.route_executor.execution_states["TR"] = run

    payload = build_network_payload(net, sim)

    # 4th sibling group present and covers every train.
    assert "route_runs_by_train_id" in payload
    rr = payload["route_runs_by_train_id"]
    assert set(rr.keys()) == set(net.trains.keys())
    assert rr["TR"] == {
        "route_id": "ROUTE_X",
        "run_number": 3,
        "state": "RUNNING",
        "display_state": "RUNNING",
        "last_error": None,
    }

    # The pre-existing assigned-routes map is still emitted (frontend reads it).
    assert "assigned_routes_by_train_id" in payload
