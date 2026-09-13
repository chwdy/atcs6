"""Meet candidate-pinning (H7) — `required_candidate_resource_keys` on
`create_path` / `extend_path`, and the `get_waiting_conflict` producer.

design_new.md "Path API migration" (:1232), "create_path() flow" (:1261),
"Path extension" (:1448), and "Meet coordination" (:1939): when the tick-start
meet calculation pins a train to one candidate route, the dispatcher must
acquire exactly that route — never falling through to an equally-reachable
alternative — and must fail closed (create: no-viable; extend: no-growth,
path untouched) when the pinned candidate no longer matches. `get_waiting_conflict`
is the canonical producer the contention snapshot reads (design_new.md
"Contention snapshot").
"""

from enums import Direction
from network import RailwayNetwork
from path_dispatcher import PathDispatcher, WaitingConflict
from runtime_payloads import (
    PathAcquisitionFailureKind,
    ResolvedRouteTarget,
)
from simulation import SimulationController
from track import Tile, TRACK_EW
from train import Train, TrainCar
from tests.segment_test_helpers import make_segment_block_with_tracks
from tests.train_test_helpers import set_train_span


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
        make_segment_block_with_tracks(
            net, bid, positions=[(a, 0), (b, 0)], name=bid, length_cm=100.0
        )
    return net


def _place(net, train_id, tile_x, block_id):
    car = TrainCar(car_id=f"{train_id}_CAR", car_type="LOCOMOTIVE", length_cm=40.0)
    train = Train(train_id=train_id, consist=[car], max_speed=60.0)
    net.add_train(train)
    net.place_train(train, tile_x, 0, Direction.EAST)
    net.block_manager.occupy_block(block_id, train_id)
    return train


def _routing_train(net, train_id="T1", tile_x=0, block_id="BLOCK_A"):
    """Place a routing train head-in-block with an EAST-facing span, returning
    (dispatcher, train)."""
    train = _place(net, train_id, tile_x, block_id)
    disp = PathDispatcher(net)
    sim = SimulationController(net, disp)
    sc = sim._get_or_create_controller(train)
    set_train_span(
        sc, (tile_x, 0), facing_direction=Direction.EAST, occupied_blocks=[block_id]
    )
    return disp, train


def _keys(disp, start, end, direction="forward"):
    """The pinned key list a meet calc would emit for the best `start -> end`
    candidate (the same shared pathfinder Plan and meet coordination use)."""
    cands = disp._pathfinder.find_ordered_walk(
        start, end, direction_constraint=direction
    )
    assert cands, f"expected a walk {start} -> {end}"
    return PathDispatcher._ordered_walk_keys(cands[0].ordered_walk)


_BOGUS_PIN = [("block", "__NO_SUCH_BLOCK__", "E", "W")]


# --- create_path -----------------------------------------------------------


def test_create_path_honors_matching_pin():
    """A pin naming the real route is acquired end-to-end."""
    net = _linear_network()
    disp, _ = _routing_train(net)
    res = disp.create_path(
        train_id="T1",
        requested_destination_block_id="BLOCK_C",
        required_candidate_resource_keys=_keys(disp, "BLOCK_A", "BLOCK_C"),
    )
    assert res.success
    assert res.reached_target
    assert disp.get_path(res.path_id).get_reservation_end_block_id() == "BLOCK_C"


def test_create_path_refuses_mismatched_pin_without_fallthrough():
    """A pin that matches no candidate fails closed with no_viable_path — even
    though the very same request succeeds unpinned, proving the dispatcher does
    not fall through to the route the meet calc did not pin."""
    # The identical request with no pin succeeds: the route is viable.
    net_ok = _linear_network()
    disp_ok, _ = _routing_train(net_ok)
    baseline = disp_ok.create_path(
        train_id="T1", requested_destination_block_id="BLOCK_C"
    )
    assert baseline.success and baseline.reached_target

    # With a non-matching pin the create is refused rather than detouring onto
    # the viable route.
    net = _linear_network()
    disp, _ = _routing_train(net)
    res = disp.create_path(
        train_id="T1",
        requested_destination_block_id="BLOCK_C",
        required_candidate_resource_keys=_BOGUS_PIN,
    )
    assert not res.success
    assert res.failure_kind is PathAcquisitionFailureKind.NO_VIABLE_PATH
    assert disp.get_active_path("T1") is None  # nothing was created


# --- extend_path -----------------------------------------------------------


def _live_path_to_b(net):
    disp, _ = _routing_train(net)
    res = disp.create_path(
        train_id="T1", requested_destination_block_id="BLOCK_B"
    )
    assert res.success and res.reached_target
    return disp, res.path_id


def test_extend_path_matches_pinned_suffix():
    """The meet calc pins the FULL head->target (A->E) route; an extend from the
    current authority end (B->E) is the forward tail of that route, so it matches
    and grows."""
    net = _linear_network()
    disp, path_id = _live_path_to_b(net)
    res = disp.extend_path(
        path_id=path_id,
        requested_destination_block_id="BLOCK_E",
        required_candidate_resource_keys=_keys(disp, "BLOCK_A", "BLOCK_E"),
    )
    assert res.success
    assert res.appended_resource_count > 0
    assert res.reached_target
    assert disp.get_path(path_id).get_reservation_end_block_id() == "BLOCK_E"


def test_extend_path_refuses_mismatched_pin_as_no_growth():
    """A non-matching pin appends nothing and leaves the live path untouched —
    the no-growth retry result, never a structural failure that would tear the
    meet-held authority down."""
    net = _linear_network()
    disp, path_id = _live_path_to_b(net)
    before = list(disp.get_path(path_id).resource_sequence)
    res = disp.extend_path(
        path_id=path_id,
        requested_destination_block_id="BLOCK_E",
        required_candidate_resource_keys=_BOGUS_PIN,
    )
    assert res.success
    assert res.appended_resource_count == 0
    assert res.reached_target is False
    # Existing authority is preserved unchanged for the next tick's retry.
    assert disp.get_path(path_id).resource_sequence == before
    assert disp.get_path(path_id).get_reservation_end_block_id() == "BLOCK_B"


# --- get_waiting_conflict --------------------------------------------------


def test_get_waiting_conflict_reports_block_holder():
    """A best-effort path truncated behind an occupied block reports that block
    as the conflict, with the occupant as the holder."""
    net = _linear_network()
    disp, _ = _routing_train(net)  # T1 head in BLOCK_A
    _place(net, "T2", 4, "BLOCK_C")  # blocker occupies BLOCK_C

    res = disp.create_path(
        train_id="T1", requested_destination_block_id="BLOCK_E"
    )
    assert res.success
    assert disp.get_path(res.path_id).get_reservation_end_block_id() != "BLOCK_E"

    conflict = disp.get_waiting_conflict(
        "T1",
        ResolvedRouteTarget("BLOCK_E", "forward"),
        False,
    )
    assert conflict == WaitingConflict(
        waiting_block_id="BLOCK_C",
        failed_resource_type="block",
        failed_resource_id="BLOCK_C",
        holding_train_ids=("T2",),
    )
    # The read-model projection returns just the block.
    assert disp.get_waiting_for_block_id(
        "T1", ResolvedRouteTarget("BLOCK_E", "forward")
    ) == "BLOCK_C"


def test_get_waiting_conflict_none_when_pinned_candidate_gone():
    """When the request is pinned to a candidate that does not match the live
    suffix, there is no conflict this read can name (fail closed → None)."""
    net = _linear_network()
    disp, _ = _routing_train(net)
    _place(net, "T2", 4, "BLOCK_C")

    res = disp.create_path(
        train_id="T1", requested_destination_block_id="BLOCK_E"
    )
    assert res.success

    conflict = disp.get_waiting_conflict(
        "T1",
        ResolvedRouteTarget("BLOCK_E", "forward"),
        False,
        required_candidate_resource_keys=_BOGUS_PIN,
    )
    assert conflict is None
