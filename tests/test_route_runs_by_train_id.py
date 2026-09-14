"""`route_runs_by_train_id` composite sibling payload
(design_new.md "API payload ownership").

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
from runtime_payloads import ErrorRecord
from train_route import RouteRunStatus
from simulation import SimulationController, build_network_payload


def test_route_run_summary_reflects_cancellation_and_terminal_states():
    """`RouteExecutor.route_run_summary_for` returns the 5 doc fields for a
    RUNNING run, reflects `display_state == "CANCELLED"` while
    `last_cancellation` is set (state stays RUNNING — a cancellation is not a
    stored state), and is None for non-RUNNING or absent runs."""
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

    # A cancellation records `last_cancellation`; display_state reads CANCELLED
    # while state stays RUNNING (design_new.md "RouteRun schema"). Every
    # cancellation kind is surfaced the same way — the reason lives on the
    # ErrorRecord.
    run.last_cancellation = ErrorRecord("operator stop", "operator_cancelled")
    cancelled = ex.route_run_summary_for("T1")
    assert cancelled["display_state"] == "CANCELLED"
    assert cancelled["state"] == "RUNNING"
    run.last_cancellation = None

    # A terminal (FINISHED) run is not reported as a RUNNING summary.
    run.current_route_status = RouteRunStatus.FINISHED
    assert ex.route_run_summary_for("T1") is None

    # Train with no run at all.
    assert ex.route_run_summary_for("UNIDENTIFIED") is None


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


def test_handle_path_cancellation_records_last_cancellation_for_display():
    """`handle_path_cancellation` stores its `error` on the run as
    `last_cancellation` so `display_state` projects CANCELLED for every
    cancellation (design_new.md "RouteRun schema")."""
    ex = RouteExecutor(RailwayNetwork())
    run = RouteExecutionState(train_id="T1", route_id="R1", started_at=0.0)
    run.current_route_status = RouteRunStatus.RUNNING
    ex.execution_states["T1"] = run

    err = ErrorRecord("off-sequence", "out_of_sequence")
    ex.handle_path_cancellation("T1", "P1", error=err)
    assert run.last_cancellation is err
    assert run.display_state == "CANCELLED"

    # A later cancellation replaces the recorded diagnostic; still CANCELLED.
    err2 = ErrorRecord("operator stop", "operator_cancelled")
    ex.handle_path_cancellation("T1", "P1", error=err2)
    assert run.last_cancellation is err2
    assert run.display_state == "CANCELLED"
