"""`route_runs_by_train_id` composite sibling payload
(design_new.md "API payload ownership").

Per the canonical "Data Flow To UI" contract, any composite network/train
snapshot must expose a 4th sibling payload group `route_runs_by_train_id`
mapping `train_id -> {route_id, run_number, state, display_state, last_error}`
for the train's currently-`RUNNING` `RouteRun` (None when the train has no
RUNNING run), produced by `RouteExecutor`. The pre-existing
`assigned_routes_by_train_id` map (which the frontend reads) is kept alongside
it.

`display_state`'s `"CANCELLED"` case is resolved in the read model from the
train's most recently terminated `Path` record plus the run's live-path state
(design_new.md "Route-run state", `display_state`) — no stored `RouteRun` field.
"""

from enums import PathStatus
from network import RailwayNetwork
from path import Path
from route_executor import RouteExecutionState
from train import Train, TrainCar
from train_route import RouteRunStatus
from path_dispatcher import PathDispatcher
from simulation import SimulationController, build_network_payload


def _terminated_path(dispatcher, train_id, path_id, status, when):
    """Inject a terminated (non-ACTIVE) Path record for `train_id`."""
    path = Path(path_id=path_id, status=status, train_id=train_id)
    path.last_status_updated_time = when
    dispatcher.paths[path_id] = path
    return path


def test_display_state_reads_cancelled_from_a_terminated_path_record():
    """`route_run_summary_for` reports `display_state == "CANCELLED"` when the
    run has no live path and the train's most recently terminated `Path` ended
    CANCELLED; state stays RUNNING (a cancellation is not a stored state)."""
    net = RailwayNetwork()
    sim = SimulationController(net, PathDispatcher(net))
    ex = sim.route_executor

    run = RouteExecutionState(
        train_id="T1", route_id="R1", started_at=0.0, run_number=7
    )
    run.current_route_status = RouteRunStatus.RUNNING
    ex.execution_states["T1"] = run

    # No terminated path yet -> plain RUNNING.
    summary = ex.route_run_summary_for("T1")
    assert summary == {
        "route_id": "R1",
        "run_number": 7,
        "state": "RUNNING",
        "display_state": "RUNNING",
        "last_error": None,
    }

    # A terminated CANCELLED path with no live path -> display_state CANCELLED,
    # state stays RUNNING.
    _terminated_path(sim.path_dispatcher, "T1", "P1", PathStatus.CANCELLED, when=1.0)
    cancelled = ex.route_run_summary_for("T1")
    assert cancelled["display_state"] == "CANCELLED"
    assert cancelled["state"] == "RUNNING"

    # A COMPLETED terminal (a normal stop) never reads CANCELLED.
    _terminated_path(sim.path_dispatcher, "T1", "P2", PathStatus.COMPLETED, when=2.0)
    assert ex.route_run_summary_for("T1")["display_state"] == "RUNNING"

    # A fresh live (ACTIVE) path returns the projection to RUNNING even with a
    # cancelled record in history.
    _terminated_path(sim.path_dispatcher, "T1", "P1", PathStatus.CANCELLED, when=3.0)
    active = Path(path_id="P3", status=PathStatus.ACTIVE, train_id="T1")
    sim.path_dispatcher.paths["P3"] = active
    assert ex.route_run_summary_for("T1")["display_state"] == "RUNNING"

    # A terminal (FINISHED) run is not reported as a RUNNING summary.
    run.current_route_status = RouteRunStatus.FINISHED
    assert ex.route_run_summary_for("T1") is None

    # Train with no run at all.
    assert ex.route_run_summary_for("UNIDENTIFIED") is None


def test_stalled_outranks_cancelled():
    """`no_reachable_task` (STALLED) outranks the CANCELLED projection: the
    run-only display_state returns STALLED before the read model considers the
    terminated-path CANCELLED case (design_new.md "Route-run state")."""
    net = RailwayNetwork()
    sim = SimulationController(net, PathDispatcher(net))
    ex = sim.route_executor

    run = RouteExecutionState(
        train_id="T1", route_id="R1", started_at=0.0, run_number=1
    )
    run.current_route_status = RouteRunStatus.RUNNING
    run.no_reachable_task = True
    ex.execution_states["T1"] = run

    _terminated_path(sim.path_dispatcher, "T1", "P1", PathStatus.CANCELLED, when=1.0)
    assert ex.route_run_summary_for("T1")["display_state"] == "STALLED"


def test_build_network_payload_emits_route_runs_sibling():
    """The composite builder emits `route_runs_by_train_id` covering every
    train, with the RUNNING summary populated, while keeping
    `assigned_routes_by_train_id`."""
    net = RailwayNetwork()
    sim = SimulationController(net)

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


def test_handle_path_cancellation_writes_no_run_field():
    """`handle_path_cancellation` writes no `RouteRun` field (design_new.md
    "Route-run state"): the display projection reads CANCELLED from the
    terminated Path record and the cancel_path event, not from stored run
    state."""
    from runtime_payloads import ErrorRecord

    net = RailwayNetwork()
    sim = SimulationController(net, PathDispatcher(net))
    ex = sim.route_executor
    run = RouteExecutionState(train_id="T1", route_id="R1", started_at=0.0)
    run.current_route_status = RouteRunStatus.RUNNING
    ex.execution_states["T1"] = run

    err = ErrorRecord("off-sequence", "out_of_sequence")
    ex.handle_path_cancellation("T1", "P1", error=err)
    # The callback stores nothing on the run; state is unchanged.
    assert run.current_route_status == RouteRunStatus.RUNNING
    assert not hasattr(run, "last_cancellation")

    # With a terminated CANCELLED path and no live path, the read model projects
    # CANCELLED.
    _terminated_path(sim.path_dispatcher, "T1", "P1", PathStatus.CANCELLED, when=1.0)
    assert ex.route_run_summary_for("T1")["display_state"] == "CANCELLED"
