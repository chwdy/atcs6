"""A stopped train arrives only once it has used up its clearance
(design_new.md step `8df5`, Stop-arrival check, arrival condition 4a).

The distance-0 reason code reads `"reservation_stop"` whenever the reservation
end is in view, so on a layout whose blocks carry no speed limit a train still
standing at the start of a fresh reservation already shows it. 4a therefore also
asks that the braking target is reached — `target_stop_cm` at or below
`RESERVATION_STOP_ARRIVAL_TOLERANCE_CM` — so that train has not arrived. Before
this, a manual train assigned a route had its path completed on the next tick
without moving, and the driver was left with no reservation to drive in.

The harness is the straight-line one from the seam-cap suite: 100 cm blocks
BLK_0..BLK_{n-1} on one E-W run. Like the rig layout the bug was found on, the
blocks carry no speed limit and the stop is close, so the braking curve to the
reservation end is the distance-0 limit while the train still stands.
"""

from constants import RESERVATION_STOP_ARRIVAL_TOLERANCE_CM
from enums import Direction, HandlePosition, PathStatus
from path_dispatcher import PathDispatcher
from simulation import SimulationController
from train import Train, TrainCar
from train_route import Route, RouteMode, RouteTask

from tests.test_block_occupancy_cap import build_line

DT = 0.1


def _train_with_stop_route(mode: str = "manual", target_index: int = 2):
    network = build_line(target_index + 2)
    for block in network.blocks.values():
        block.speed_limit_kmh = None
    dispatcher = PathDispatcher(network)
    sim = SimulationController(network, dispatcher)
    train = Train(
        train_id="T",
        consist=[TrainCar(car_id="c1", car_type="LOCOMOTIVE", length_cm=40.0)],
        max_speed=300.0,
    )
    network.add_train(train)
    controller = sim._get_or_create_controller(train)
    controller.physics_sync.current_span = network.place_train(
        train, 0, 0, Direction.EAST
    )
    sim._get_or_create_physics(train)
    network.block_manager.occupy_block("BLK_0", "T")

    target = f"BLK_{target_index}"
    route = Route(
        route_id="R",
        name="R",
        mode=RouteMode.ONE_OFF,
        tasks=[
            RouteTask(
                task_kind="stop",
                task_location={"block_id": target},
                task_direction="either",
            )
        ],
    )
    sim.route_executor.register_route(route)
    assert controller.commands.power_on_cmd().success
    if mode == "ato":
        assert controller.commands.set_mode("ato").success
    sim.route_executor.request_route_assignment("T", "R")
    return sim, dispatcher, controller, target


def _active_path(dispatcher):
    path = dispatcher.get_active_path("T")
    return path if path is not None and path.status == PathStatus.ACTIVE else None


def _tick(sim, n=1):
    for _ in range(n):
        sim.update(DT)


def test_a_standing_manual_train_keeps_its_fresh_path():
    sim, dispatcher, controller, target = _train_with_stop_route()

    _tick(sim)
    path = _active_path(dispatcher)
    assert path is not None, "a manual train acquires its reservation at once"
    assert path.get_reservation_end_block_id() == target

    lookahead = controller._compute_canonical_lookahead()["lookahead"]
    assert lookahead["speed_curves"][0]["atc_limit_reason_code"] == "reservation_stop"
    assert lookahead["target_stop_cm"] > RESERVATION_STOP_ARRIVAL_TOLERANCE_CM

    _tick(sim, 20)
    assert controller.is_stopped()
    assert not controller.is_stopped_at_reservation_end()
    assert _active_path(dispatcher) is path, "standing still is not arriving"
    assert sim.route_executor.assigned_route_id_for("T") == "R"


def test_a_manual_driver_who_drives_up_to_the_stop_arrives():
    sim, dispatcher, controller, target = _train_with_stop_route()
    _tick(sim)
    assert controller.commands.set_handle(HandlePosition.P5).success

    # ATC caps the notch and brings the train to rest at its braking target;
    # the stop then completes and the one-off route finishes.
    for _ in range(int(120 / DT)):
        _tick(sim)
        if sim.route_executor.assigned_route_id_for("T") is None:
            break
    assert sim.route_executor.assigned_route_id_for("T") is None
    assert dispatcher.get_active_path("T") is None


def test_a_paused_ato_train_short_of_its_stop_has_not_arrived():
    sim, dispatcher, controller, target = _train_with_stop_route(
        mode="ato", target_index=5
    )
    assert controller.commands.ato_go().success
    _tick(sim, 10)
    assert not controller.is_stopped(), "the engaged train is under way"

    assert controller.commands.ato_stop().success
    for _ in range(int(30 / DT)):
        _tick(sim)
        if controller.is_stopped():
            break
    assert controller.is_stopped()
    _tick(sim, 5)

    path = _active_path(dispatcher)
    assert path is not None, "ATO stop keeps the reservation; it is not an arrival"
    assert path.get_reservation_end_block_id() == target
    assert sim.route_executor.assigned_route_id_for("T") == "R"


def test_an_ato_train_that_brakes_to_its_stop_still_arrives():
    sim, dispatcher, controller, target = _train_with_stop_route(mode="ato")
    assert controller.commands.ato_go().success

    for _ in range(int(120 / DT)):
        _tick(sim)
        if sim.route_executor.assigned_route_id_for("T") is None:
            break
    assert sim.route_executor.assigned_route_id_for("T") is None
    assert dispatcher.get_active_path("T") is None


def test_arrival_tolerance_is_two_centimetres_at_or_below(monkeypatch):
    """4a's distance test reads "at or below `RESERVATION_STOP_ARRIVAL_TOLERANCE_CM`
    — `2.0` cm" (design_new.md "Stop-arrival check"). The train stands short of
    its terminal block, so 4b cannot answer for it; only the distance to the
    braking target decides. Everything but that one distance comes from the
    train's own lookahead, which a train cannot be parked to the centimetre to
    produce."""
    assert RESERVATION_STOP_ARRIVAL_TOLERANCE_CM == 2.0

    sim, dispatcher, controller, target = _train_with_stop_route()
    _tick(sim)
    assert _active_path(dispatcher) is not None
    assert controller.is_stopped()
    head = controller._get_head_tile()
    assert sim.network.tile_to_block_id(head[0], head[1]) != target

    real_lookahead = controller._compute_canonical_lookahead

    def stand_at(target_stop_cm):
        def lookahead():
            payload = real_lookahead()
            payload["lookahead"] = {
                **payload["lookahead"], "target_stop_cm": target_stop_cm
            }
            return payload

        monkeypatch.setattr(controller, "_compute_canonical_lookahead", lookahead)
        reason = lookahead()["lookahead"]["speed_curves"][0]["atc_limit_reason_code"]
        assert reason == "reservation_stop"
        return controller.is_stopped_at_reservation_end()

    assert stand_at(RESERVATION_STOP_ARRIVAL_TOLERANCE_CM) is True
    assert stand_at(0.0) is True
    assert stand_at(RESERVATION_STOP_ARRIVAL_TOLERANCE_CM + 0.5) is False
