"""Contention snapshot and meet coordination (design_new.md "Contention
snapshot", "Meet coordination", "Wedge detection").

The end-to-end cases run two trains that swap ends of the `demo_test` passing
loop: TEST_TRAIN starts in p1 bound for p2, T2 starts in p2 bound for p1. The
two roads between them are the SHORT and LONG branches, and both throats are
single track, so the trains' routes run over the throats in opposite
directions — a meet. Holding points on the loop roads are the passing places.
"""

from types import SimpleNamespace

from block import Block
from enums import Direction
from meet_coordination import MeetEntry, _Route, _Step, contested_blocks
from network import RailwayNetwork
from path_dispatcher import PathDispatcher, WaitingConflict
from route_executor import ContentionEdge, ContentionSnapshot, RouteExecutor
from runtime_payloads import ResolvedRouteTarget
from simulation import SimulationController
from track import TRACK_EW, Tile
from train import Train, TrainCar
from train_route import PlatformTarget, Route, RouteMode, RouteTask
from tests.journey.scenes.demo_test_scene import (
    LONG_BRANCH_BLOCKS,
    SHORT_BRANCH_BLOCKS,
    DemoTestScene,
)
from tests.segment_test_helpers import make_segment_block_with_tracks

P1_BLOCK = "PLAT_532737_BLOCK"
P2_BLOCK = "PLAT_513670_BLOCK"
LOOP_HOLDING_POINTS = ("BLK_2FD0FF", "BLK_2F9133", "BLK_2FEE7D", "BLK_2FDE9B")


# --- contested blocks (pure) ----------------------------------------------


def _route(*blocks):
    return _Route(
        steps=[_Step("block", b, d, 10.0) for b, d in blocks], keys=(), rank=0
    )


def test_contested_blocks_are_those_run_in_opposite_directions():
    a = _route(("B1", "forward"), ("B2", "forward"), ("B3", "forward"))
    b = _route(("B5", "reverse"), ("B2", "reverse"), ("B0", "reverse"))
    assert contested_blocks(a, b) == {"B2"}


def test_a_shared_block_run_the_same_way_is_not_contested():
    a = _route(("B1", "forward"), ("B2", "forward"))
    b = _route(("B2", "forward"), ("B3", "forward"))
    assert contested_blocks(a, b) == set()


def test_disjoint_routes_contest_nothing():
    a = _route(("B1", "forward"), ("B2", "forward"))
    b = _route(("B7", "reverse"), ("B8", "reverse"))
    assert contested_blocks(a, b) == set()


# --- contention snapshot (real executor, stubbed inputs) ------------------


def _executor():
    return RouteExecutor(RailwayNetwork())


def test_contention_snapshot_emits_one_edge_per_waiter_holder():
    ex = _executor()
    # T1 and T2 are both blocked on B_X (held by H); T3 is not blocked. The
    # snapshot emits one edge per (waiter, holder) in runnable order.
    conflicts = {
        "T1": WaitingConflict("B_X", "block", "B_X", ("H",)),
        "T2": WaitingConflict("B_X", "block", "B_X", ("H",)),
        "T3": None,
    }
    ex._waiting_conflict_for = lambda tid: conflicts.get(tid)
    snap = ex._build_contention_snapshot(["T1", "T2", "T3"])
    assert snap.edges == [
        ContentionEdge("B_X", "T1", "H"),
        ContentionEdge("B_X", "T2", "H"),
    ]


def test_contention_snapshot_one_edge_per_holder():
    ex = _executor()
    # A single waiter blocked by two holders (e.g. two lock owners on a switch)
    # yields one edge per holder.
    ex._waiting_conflict_for = lambda tid: (
        WaitingConflict("B_HELD", "switch", "SW1", ("HA", "HB"))
        if tid == "T1"
        else None
    )
    snap = ex._build_contention_snapshot(["T1"])
    assert snap.edges == [
        ContentionEdge("B_HELD", "T1", "HA"),
        ContentionEdge("B_HELD", "T1", "HB"),
    ]


# --- wedge edges for steered trains ---------------------------------------


def test_a_train_standing_on_its_passing_place_contributes_no_wedge_edge():
    ex = _executor()
    ex.network.blocks["HP"] = Block(block_id="HP", length_cm=40.0)
    ex.network.blocks["HP"].occupied_by = "W"
    ex.contention_snapshot = ContentionSnapshot(
        edges=[ContentionEdge("HP", "W", "G"), ContentionEdge("X", "G", "W")]
    )
    ex.meet_assignment = {"W": MeetEntry(meet_ceiling_block_id="HP")}
    assert ex._wedge_edges(["G", "W"]) == [ContentionEdge("X", "G", "W")]


def test_a_steered_trains_wedge_edges_come_from_its_chosen_walk():
    ex = _executor()
    keys = (("block", "A", "W", "E"),)
    ex.meet_assignment = {"G": MeetEntry(candidate_resource_keys=keys)}
    ex.contention_snapshot = ContentionSnapshot(edges=[ContentionEdge("Q", "G", "OLD")])
    target = ResolvedRouteTarget("DEST", "forward")
    # A steered train's wedge edges aim at the target Plan's next call uses
    # (design_new.md "Meet coordination", The next call's target).
    ex.next_call_target = lambda tid: target
    calls = []

    def get_waiting_conflict(train_id, stored, manual, **kwargs):
        calls.append((train_id, stored, kwargs))
        return WaitingConflict("A", "block", "A", ("NEW",))

    ex.path_dispatcher = SimpleNamespace(get_waiting_conflict=get_waiting_conflict)
    assert ex._wedge_edges(["G"]) == [ContentionEdge("A", "G", "NEW")]
    assert calls == [
        (
            "G",
            target,
            {"reservation_cap_block_id": None, "required_candidate_resource_keys": list(keys)},
        )
    ]


# --- kept ways: acquisition, Preemption, waiting conflict -----------------

LINE = ["BLK_1", "BLK_2", "BLK_3", "BLK_4", "BLK_5"]


def _line():
    """Five two-tile blocks in a straight east-west line, T1 on BLK_1."""
    network = RailwayNetwork()
    for x in range(2 * len(LINE)):
        network.add_track(Tile(x, 0, track_bits=TRACK_EW))
    for i, block_id in enumerate(LINE):
        make_segment_block_with_tracks(
            network, block_id, positions=[(2 * i, 0), (2 * i + 1, 0)],
            name=block_id, length_cm=100.0,
        )
    train = Train(
        train_id="T1",
        consist=[TrainCar(car_id="C1", car_type="LOCOMOTIVE", length_cm=20.0)],
        max_speed=60.0,
    )
    network.add_train(train)
    dispatcher = PathDispatcher(network)
    sim = SimulationController(network, dispatcher)
    controller = sim._get_or_create_controller(train)
    controller.physics_sync.current_span = network.place_train(
        train, 1, 0, direction=Direction.EAST
    )
    controller._apply_head_frame("end_a")
    return network, dispatcher, sim


def test_a_kept_way_stops_another_trains_reservation_before_it():
    network, dispatcher, sim = _line()
    sim.route_executor._meet_way_holders = {("block", "BLK_4"): "T9"}

    result = dispatcher.create_path(train_id="T1", requested_destination_block_id="BLK_5")

    assert result.success and result.reached_target is False
    assert dispatcher.get_active_path("T1").get_reservation_end_block_id() == "BLK_3"
    assert network.blocks["BLK_4"].reserved_by_path is None


def test_the_waiting_conflict_names_the_train_the_way_is_kept_for():
    _network, dispatcher, sim = _line()
    dispatcher.create_path(train_id="T1", requested_destination_block_id="BLK_2")
    sim.route_executor._meet_way_holders = {("block", "BLK_3"): "T9"}

    conflict = dispatcher.get_waiting_conflict(
        "T1", ResolvedRouteTarget("BLK_5", "forward")
    )

    assert conflict is not None
    assert conflict.failed_resource_id == "BLK_3"
    assert conflict.holding_train_ids == ("T9",)


def test_a_trains_own_kept_way_does_not_stop_it():
    network, dispatcher, sim = _line()
    sim.route_executor._meet_way_holders = {("block", "BLK_4"): "T1"}

    result = dispatcher.create_path(train_id="T1", requested_destination_block_id="BLK_5")

    assert result.reached_target is True


def test_giving_back_reservation_past_the_passing_place():
    network, dispatcher, sim = _line()
    dispatcher.create_path(train_id="T1", requested_destination_block_id="BLK_5")
    executor = sim.route_executor
    executor.meet_assignment = {"T1": MeetEntry(meet_ceiling_block_id="BLK_3")}
    executor.meet_waiting_for = {"T1": "T9"}

    executor._give_back_past_meet_ceiling("T1")

    assert dispatcher.get_active_path("T1").get_reservation_end_block_id() == "BLK_3"
    assert network.blocks["BLK_4"].reserved_by_path is None
    assert network.blocks["BLK_5"].reserved_by_path is None


# --- end to end on the demo_test passing loop -----------------------------


def _passing_loop(holding_points=LOOP_HOLDING_POINTS, t2_priority=5):
    """TEST_TRAIN in p1 bound for p2 and T2 in p2 bound for p1, both on ATO.

    One tick runs with neither train engaged, so Plan allocates both runs
    before either reserves anything: a train with no `RUNNING` run is never in
    a meet."""
    fixture = DemoTestScene().build()
    sim, network = fixture.sim_controller, fixture.network
    for block_id in holding_points:
        network.blocks[block_id].is_holding_point = True
    t2 = sim.add_train(
        train_id="T2",
        starting_block_id=P2_BLOCK,
        initial_direction="NORTH",
        max_speed_kmh=60.0,
        length_cm=50.0,
        train_type="LOCOMOTIVE",
        initial_cab="A",
        start_tile=(6, 5),
    )
    t2.min_radius_cm = 31.5
    t2.speed_profile_id = network.trains[fixture.train_id].speed_profile_id
    route = Route(
        route_id="P2_TO_P1", name="p2 to p1", mode=RouteMode.ONE_OFF, priority=t2_priority
    )
    route.add_task(
        RouteTask(
            task_kind="stop",
            task_location=PlatformTarget(platform_id="PLAT_532737"),
            task_direction="either",
        )
    )
    sim.route_executor.register_route(route)
    route.assign_train("T2")
    c1 = sim.get_or_create_controller(fixture.train_id)
    c2 = sim.get_or_create_controller("T2")
    c2.commands.power_on_cmd()
    c2.commands.set_mode("ato")
    c1.commands.ato_stop()
    sim.update(0.1)
    c1.commands.ato_go()
    c2.commands.ato_go()
    return sim, network, c1, c2


def _events(sim, operation):
    return [n for n in sim.notification_center.peek_recent(500) if n.operation == operation]


def test_the_waiting_train_holds_at_its_passing_place_while_the_other_goes_by():
    sim, network, c1, c2 = _passing_loop()
    dispatcher = sim.path_dispatcher

    sim.update(0.1)
    entries = dict(sim.route_executor.meet_assignment)
    # Equal priority, neither reserved into the throats yet: T2, later in the
    # runnable order, waits at its nearest passing place, the first holding
    # point on the short road. TEST_TRAIN is steered onto the long road.
    assert entries["T2"].meet_ceiling_block_id == "BLK_2FDE9B"
    assert entries["TEST_TRAIN"].meet_ceiling_block_id is None
    assert entries["TEST_TRAIN"].candidate_resource_keys
    assert dispatcher.get_active_path("T2").get_reservation_end_block_id() == "BLK_2FDE9B"
    first = [e.resource_id for e in dispatcher.get_active_path("TEST_TRAIN").resource_sequence]
    assert "BLK_2FD0FF" in first and not set(first) & set(SHORT_BRANCH_BLOCKS)

    t1_blocks = set()
    for _ in range(1500):
        sim.update(0.1)
        path = dispatcher.get_active_path("TEST_TRAIN")
        if path is not None:
            t1_blocks |= {e.resource_id for e in path.resource_sequence}
        if (
            c1._get_current_block_id() == P2_BLOCK
            and c2._get_current_block_id() == P1_BLOCK
            and c1.is_stopped()
            and c2.is_stopped()
        ):
            break
    else:
        raise AssertionError("the trains never finished swapping ends of the loop")

    # The train that goes never took the waiting train's road.
    assert not t1_blocks & set(SHORT_BRANCH_BLOCKS)
    assert set(LONG_BRANCH_BLOCKS) <= t1_blocks
    holds = _events(sim, "compute_meet_assignment")
    assert [h.payload["train_id"] for h in holds] == ["T2"]
    assert not _events(sim, "build_wedge_report")
    # The meet ended by itself once the trains had passed.
    assert sim.route_executor.meet_assignment == {}


def test_the_lower_priority_train_waits():
    sim, _network, _c1, _c2 = _passing_loop(t2_priority=8)

    sim.update(0.1)

    entries = sim.route_executor.meet_assignment
    assert entries["TEST_TRAIN"].meet_ceiling_block_id is not None
    assert entries["T2"].meet_ceiling_block_id is None
    assert sim.route_executor.meet_waiting_for == {"TEST_TRAIN": "T2"}


def test_with_no_place_to_pass_neither_train_gets_an_entry_and_both_are_named():
    sim, _network, _c1, _c2 = _passing_loop(holding_points=())

    sim.update(0.1)

    assert sim.route_executor.meet_assignment == {}
    warnings = _events(sim, "compute_meet_assignment")
    assert len(warnings) == 1
    named = {e.entity_id for e in warnings[0].entities}
    assert named == {"TEST_TRAIN", "T2"}
    assert warnings[0].error.origin == "meet_hold"


# --- the next call's target -----------------------------------------------


def _stub_next_call(ex, ahead_target):
    armed_target = ResolvedRouteTarget("W1", "forward")
    projection = SimpleNamespace(
        walk_position=0, task_kind="waypoint", resolved_target=armed_target
    )
    ahead = (
        SimpleNamespace(walk_position=1, task_kind="waypoint", resolved_target=ahead_target)
        if ahead_target is not None
        else projection
    )
    ex.execution_state_for = lambda tid: SimpleNamespace()
    ex._get_active_path_id = lambda tid: "P1"
    ex.find_armed_task = lambda tid: SimpleNamespace(stored_resolved_target=armed_target)
    ex._resolve_run_route = lambda tid: (SimpleNamespace(), SimpleNamespace(), None)
    ex._find_next_task_target = lambda tid, **kw: projection
    ex._apply_waypoint_lookahead = lambda train, route, state, proj: ahead
    return armed_target


def test_the_next_call_target_is_the_lookahead_target_when_it_arms():
    """On the tick the reservation reaches the armed waypoint, Plan aims at the
    next task, so meet coordination does too (design_new.md "Meet
    coordination", The next call's target)."""
    ex = _executor()
    w2 = ResolvedRouteTarget("W2", "forward")
    _stub_next_call(ex, w2)
    assert ex.next_call_target("T1") == w2


def test_the_next_call_target_is_the_armed_target_otherwise():
    ex = _executor()
    armed = _stub_next_call(ex, None)
    assert ex.next_call_target("T1") == armed
