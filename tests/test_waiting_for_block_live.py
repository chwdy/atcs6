"""Live composition of ``waiting_for_block_id`` (design_new.md "Data Flow To UI").

Per the canonical "Data Flow To UI" / "Contention snapshot" contract,
``waiting_for_block_id`` is NOT derived by ``get_active_path_progress(...)``:
that projection returns only the five dispatcher-owned path fields. The block a
blocked waiter is stuck at needs the complete route-owned request (the run's
``resolved_target`` plus its effective acquisition mode / manual override), so
the composite read-model assembly composes it there from the route-supplied
``RouteExecutor.waiting_for_block_id_for(...)`` call. That method obtains the
target from ``find_armed_task(...)`` and forwards the request to the canonical
``PathDispatcher.get_waiting_for_block_id(...)`` projection, which re-runs the
shared pathfinder/eligibility walk live and reports the first forward resource
that fails acquisition, or ``None`` when the authority reaches its target.

These tests are route-backed: they seed a RUNNING run with an armed task (the
route-owned target), exactly as the read-model assembly does, and pin that the
projection names the true blocker and clears the moment the obstruction frees.
They also pin that the requested-target fields no longer ride on the dispatcher
progress payload — they moved out to the route side.
"""

from network import RailwayNetwork
from track import Tile, TRACK_EW
from train import Train, TrainCar
from enums import Direction
from path_dispatcher import PathDispatcher
from route_executor import RouteExecutor, RouteExecutionState
from runtime_payloads import ResolvedRouteTarget
from train_route import RouteRunStatus, RouteTaskRun
from simulation import SimulationController
from tests.train_test_helpers import set_train_span
from tests.segment_test_helpers import make_segment_block_with_tracks


def _linear_network():
    """A - B - C - D - E linear EW corridor, two tiles per block."""
    net = RailwayNetwork()
    for x in range(0, 10):
        net.add_track(Tile(x, 0, track_bits=TRACK_EW))
    for bid, (a, b) in [
        ("BLOCK_A", (0, 1)),
        ("BLOCK_B", (2, 3)),
        ("BLOCK_C", (4, 5)),
        ("BLOCK_D", (6, 7)),
        ("BLOCK_E", (8, 9)),
    ]:
        make_segment_block_with_tracks(net, bid, positions=[(a, 0), (b, 0)], name=bid, length_cm=100.0
        )
    return net


def _place(net, train_id, tile_x, block_id):
    car = TrainCar(car_id=f"{train_id}_CAR", car_type="LOCOMOTIVE", length_cm=40.0)
    train = Train(train_id=train_id, consist=[car], max_speed=60.0)
    net.add_train(train)
    net.place_train(train, tile_x, 0, Direction.EAST)
    net.block_manager.occupy_block(block_id, train_id)
    return train


def _route_backed(net, disp, target="BLOCK_E", direction="forward"):
    """A RouteExecutor carrying a RUNNING run whose armed task points at
    ``target``, so ``waiting_for_block_id_for("T1")`` has a route-owned
    ``resolved_target`` to forward — the same request the composite read-model
    assembly builds. The run has no shared ``Route`` registered, so effective
    acquisition mode falls back to BEST_EFFORT and manual_override to False,
    matching a plain best-effort run.
    """
    ex = RouteExecutor(net, path_dispatcher=disp)
    run = RouteExecutionState(train_id="T1", route_id="R1", started_at=0.0)
    run.current_route_status = RouteRunStatus.RUNNING
    run.task_runs = [
        RouteTaskRun(
            task_index=0,
            resolved_target=ResolvedRouteTarget(target, direction),
        )
    ]
    ex.execution_states["T1"] = run
    return ex


def test_waiting_for_block_id_reports_live_blocker():
    """A best-effort path truncated at create_path time: the route-supplied
    ``waiting_for_block_id_for`` names the occupied block ahead live, and clears
    once that block frees — with no ``extend_path`` call refreshing a field."""
    net = _linear_network()
    t1 = _place(net, "T1", 0, "BLOCK_A")  # routing train, head in BLOCK_A
    _place(net, "T2", 4, "BLOCK_C")  # blocker occupying BLOCK_C

    disp = PathDispatcher(net)
    sim = SimulationController(net, disp)
    sc = sim._get_or_create_controller(t1)
    set_train_span(
        sc, (0, 0), facing_direction=Direction.EAST, occupied_blocks=["BLOCK_A"]
    )

    # Route A -> E; BLOCK_C is occupied, so best-effort create truncates short.
    res = disp.create_path(
        train_id="T1", requested_destination_block_id="BLOCK_E"
    )
    assert res.success
    path = disp.get_path(res.path_id)
    # Truncated short of the requested target (authority did not reach E).
    assert path.get_authority_end_block_id() != "BLOCK_E"

    ex = _route_backed(net, disp, target="BLOCK_E")
    # The route supplies the target; the dispatcher runs the walk and names the
    # occupied block that stops forward growth.
    assert ex.waiting_for_block_id_for("T1") == "BLOCK_C"

    # The requested-target fields and waiting_for_block_id are no longer part of
    # the dispatcher-owned progress projection — they moved to the route side
    # (design_new.md "Data Flow To UI").
    progress = disp.get_active_path_progress("T1")
    assert progress is not None
    assert "waiting_for_block_id" not in progress
    assert "requested_destination_block_id" not in progress
    assert "requested_destination_direction" not in progress

    # Free the blocker: the suffix toward E becomes acquirable, so the live
    # composition clears to None on the very next read (no extend_path needed).
    net.block_manager.release_block("BLOCK_C")
    assert ex.waiting_for_block_id_for("T1") is None


def test_waiting_for_block_id_none_when_target_reached():
    """When the authority already reaches its requested target the route-supplied
    projection is None (nothing is blocking it)."""
    net = _linear_network()
    t1 = _place(net, "T1", 0, "BLOCK_A")

    disp = PathDispatcher(net)
    sim = SimulationController(net, disp)
    sc = sim._get_or_create_controller(t1)
    set_train_span(
        sc, (0, 0), facing_direction=Direction.EAST, occupied_blocks=["BLOCK_A"]
    )

    # Nothing ahead is occupied; A -> C succeeds end-to-end.
    res = disp.create_path(
        train_id="T1", requested_destination_block_id="BLOCK_C"
    )
    assert res.success
    path = disp.get_path(res.path_id)
    assert path.get_authority_end_block_id() == "BLOCK_C"

    ex = _route_backed(net, disp, target="BLOCK_C")
    assert ex.waiting_for_block_id_for("T1") is None
