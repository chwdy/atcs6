"""Deleting a train (design_new.md "Train Entity", Deleting a train).

`SimulationController.remove_train(train_id)` runs only on a train with nothing
left to tear down: it is refused for an unidentified train, a train that is not
OFF, and one still holding a path or a RUNNING route run. Blocks the train held
that still read current become an unidentified train. A deletion it accepts
leaves nothing keyed on the `train_id` — no route assignment, held request,
block or switch occupancy, controller, or physics instance — so a later train
with the same id starts fresh.

Run with: ``/opt/homebrew/anaconda3/envs/general/bin/python -m pytest
tests/test_train_deletion.py -q``
"""

import threading
from datetime import datetime

import pytest

from app_state import AppState
from constants import OCCUPANCY_OFF_THRESHOLD
from enums import Direction, TrainStatus
from path_dispatcher import PathDispatcher
from persistence import PersistenceManager
from services import ValidationError, train_service
from simulation import SimulationController
from switch import Switch
from train import Train, TrainCar
from train_route import Route, RouteMode, RouteTask

from tests.test_block_occupancy_cap import build_line
from tests.test_unidentified_trains import FakeDetector, build_sim, only_unidentified, tick


def _state_for(sim):
    return AppState(
        network=sim.network,
        path_dispatcher=sim.path_dispatcher,
        sim_controller=sim,
        persistence_manager=PersistenceManager(storage_dir="layouts"),
        simulation_lock=threading.Lock(),
        simulation_running=False,
        simulation_speed=1.0,
        last_update_time=datetime.now(),
    )


def _line_sim():
    network = build_line(8)
    sim = SimulationController(network, PathDispatcher(network))
    return sim, _state_for(sim)


def _place(sim, train_id="T", x=0):
    train = Train(
        train_id=train_id,
        consist=[TrainCar(car_id=f"{train_id}_c1", car_type="LOCOMOTIVE", length_cm=40.0)],
        max_speed=80.0,
    )
    sim.network.add_train(train)
    controller = sim._get_or_create_controller(train)
    controller.physics_sync.current_span = sim.network.place_train(
        train, x, 0, Direction.EAST
    )
    sim._get_or_create_physics(train)
    sim.network.block_manager.occupy_block(f"BLK_{x}", train_id)
    return train, controller


def _route(sim, route_id, block_id, mode=RouteMode.CIRCULAR):
    route = Route(
        route_id=route_id,
        name=route_id,
        mode=mode,
        tasks=[
            RouteTask(
                task_kind="stop",
                task_location={"block_id": block_id},
                task_direction="either",
            )
        ],
    )
    sim.route_executor.register_route(route)
    return route


def _run_then_power_off(sim, controller):
    """Drive the train part-way down its route, stop it, and power it off, so
    it is OFF with no path and no run, standing on the blocks it reached."""
    assert controller.commands.set_mode("ato").success
    assert controller.commands.ato_go().success
    for _ in range(30):
        sim.update(0.1)
    assert controller.commands.ato_stop().success
    for _ in range(400):
        sim.update(0.1)
        if controller.is_stopped():
            break
    assert controller.commands.power_off_cmd().success
    for _ in range(5):
        sim.update(0.1)
    assert controller.train_status == TrainStatus.OFF


def _blocks_naming(network, train_id):
    return sorted(
        block_id
        for block_id, block in network.blocks.items()
        if train_id in (block.occupied_by, block.reserved_for_train)
    )


# ---------------------------------------------------------------- refusals


def test_a_powered_train_is_refused_and_nothing_changes():
    sim, state = _line_sim()
    train, controller = _place(sim)
    assert controller.commands.set_mode("manual").success

    with pytest.raises(ValidationError, match="off"):
        train_service.delete_train(state, "T")

    assert sim.network.trains["T"] is train
    assert sim.controllers["T"] is controller
    assert sim.network.blocks["BLK_0"].occupied_by == "T"


def test_an_off_train_with_a_running_route_run_is_refused():
    """After a fatal-halt clear an OFF train keeps its RUNNING run until an
    ordinary teardown ends it; deletion waits for that teardown."""
    sim, state = _line_sim()
    train, _controller = _place(sim)
    route = _route(sim, "R", "BLK_6")
    rex = sim.route_executor
    assert rex.request_route_assignment("T", "R").success
    rex._ensure_run_for(train, route)
    assert rex.execution_state_for("T") is not None

    with pytest.raises(ValidationError, match="running route run"):
        train_service.delete_train(state, "T")

    assert "T" in sim.network.trains
    assert "T" in route.assigned_train_ids


def test_an_unidentified_train_is_refused():
    _network, sim, detector = build_sim()
    detector.current = {"BLK_1": True}
    tick(sim)
    unidentified_id = only_unidentified(sim.network)

    with pytest.raises(ValidationError, match="unidentified"):
        train_service.delete_train(_state_for(sim), unidentified_id)
    with pytest.raises(ValueError, match="unidentified"):
        sim.remove_train(unidentified_id)

    assert unidentified_id in sim.network.trains
    assert sim.network.blocks["BLK_1"].occupied_by == unidentified_id


class WiredDetector(FakeDetector):
    """A hand-driven detector that, like a real one, answers for every block."""

    def __init__(self, block_ids):
        super().__init__()
        self._block_ids = set(block_ids)

    def detected_block_ids(self):
        return set(self._block_ids)


def _wired_sim(num_blocks=3):
    network, _sim, _detector = build_sim(num_blocks)
    detector = WiredDetector(network.blocks)
    sim = SimulationController(network, PathDispatcher(network), input_interface=detector)
    sim.unidentified_clock = lambda: sim._fake_clock
    sim._fake_clock = 0.0
    return network, sim, detector, _state_for(sim)


def _adopt(sim, network, detector, blocks, new_id, length_cm=50.0):
    detector.current = {**detector.current, **{b: True for b in blocks}}
    tick(sim)
    unidentified = [
        t.train_id for t in network.trains.values()
        if t.is_unidentified and network.blocks[blocks[0]].occupied_by == t.train_id
    ]
    assert len(unidentified) == 1
    sim.adopt_unidentified_train(
        unidentified[0], new_id,
        max_speed_kmh=80.0, length_cm=length_cm, direction="EAST", cab="A",
    )
    assert all(network.blocks[b].occupied_by == new_id for b in blocks)


def test_a_train_whose_locomotive_is_still_on_the_track_becomes_an_unidentified_train():
    """A train placed wrong is deleted while its locomotive stands on the track.
    Released, the block would read FREE under the locomotive with no later entry
    to correct it, so it goes to an unidentified train, which the operator then
    adopts with the right front end."""
    network, sim, detector, state = _wired_sim()
    _adopt(sim, network, detector, ["BLK_1"], "T1")

    assert train_service.delete_train(state, "T1") is True

    assert "T1" not in network.trains
    assert "T1" not in sim.controllers
    holder = network.blocks["BLK_1"].occupied_by
    assert holder is not None and holder != "T1"
    assert network.trains[holder].is_unidentified is True
    event = sim.notification_center.latest_by_operation("remove_train")
    assert event.payload["unidentified_train_ids"] == [holder]
    assert "BLK_1" in event.message

    # Later reads keep it: the block still reads current and is held.
    tick(sim, times=5)
    assert network.blocks["BLK_1"].occupied_by == holder
    assert only_unidentified(network) == holder

    # The operator places the train again by adopting it.
    sim.adopt_unidentified_train(
        holder, "T1", max_speed_kmh=80.0, length_cm=50.0, direction="WEST", cab="A",
    )
    assert network.blocks["BLK_1"].occupied_by == "T1"
    assert network.trains["T1"].is_unidentified is False


def test_a_deleted_trains_current_never_goes_to_the_train_next_to_it():
    """A stopped train on the next block touches the deleted train's block, but
    deleting a train removes a record, not a locomotive: the block goes to an
    unidentified train, never to the train left."""
    network, sim, detector, state = _wired_sim(5)
    # One locomotive across BLK_1 and BLK_2: adopt a short consist on BLK_1,
    # and BLK_2 goes back to an unidentified train, adopted as T2.
    detector.current = {"BLK_1": True, "BLK_2": True}
    tick(sim)
    sim.adopt_unidentified_train(
        only_unidentified(network), "T1", max_speed_kmh=80.0, length_cm=50.0,
        direction="EAST", cab="A", head_tile=(1, 0),
    )
    sim.adopt_unidentified_train(
        only_unidentified(network), "T2", max_speed_kmh=80.0, length_cm=50.0,
        direction="EAST", cab="A",
    )
    assert network.blocks["BLK_1"].occupied_by == "T1"
    assert network.blocks["BLK_2"].occupied_by == "T2"

    assert train_service.delete_train(state, "T1") is True

    holder = network.blocks["BLK_1"].occupied_by
    assert holder not in (None, "T1", "T2")
    assert network.trains[holder].is_unidentified is True
    assert network.blocks["BLK_2"].occupied_by == "T2"
    tick(sim, times=5)
    assert network.blocks["BLK_1"].occupied_by == holder


def test_a_ghosted_train_leaves_no_unidentified_train():
    """Once the locomotive is lifted off, the train is ghosted, its teardown
    leaves it OFF, and deleting it frees its blocks: nothing reads current."""
    network, sim, detector, state = _wired_sim()
    _adopt(sim, network, detector, ["BLK_1"], "T1")

    detector.current = {}
    tick(sim, times=OCCUPANCY_OFF_THRESHOLD + 2)
    assert sim.controllers["T1"].presence_unknown is True
    assert sim.controllers["T1"].train_status == TrainStatus.OFF

    assert train_service.delete_train(state, "T1") is True
    assert "T1" not in network.trains
    assert network.blocks["BLK_1"].occupied_by is None
    assert not [t for t in network.trains.values() if t.is_unidentified]
    event = sim.notification_center.latest_by_operation("remove_train")
    assert event.payload["unidentified_train_ids"] == []


# ------------------------------------------------------------ what it does


def test_deleting_an_off_train_leaves_nothing_under_its_id():
    sim, state = _line_sim()
    _train, controller = _place(sim)
    route = _route(sim, "R", "BLK_6")
    rex = sim.route_executor
    assert rex.request_route_assignment("T", "R").success
    _run_then_power_off(sim, controller)
    # Assigned again while OFF: a route needs no power.
    assert rex.request_route_assignment("T", "R").success
    held = _blocks_naming(sim.network, "T")
    assert held  # it still stands on the blocks it reached

    assert train_service.delete_train(state, "T") is True

    assert "T" not in sim.network.trains
    assert "T" not in sim.controllers
    assert "T" not in sim.physics_instances
    assert "T" not in route.assigned_train_ids
    assert rex.assigned_route_id_for("T") is None
    assert "R" in rex.route_registry  # a CIRCULAR route outlives its train
    assert _blocks_naming(sim.network, "T") == []
    event = sim.notification_center.latest_by_operation("remove_train")
    assert event is not None
    assert event.has_entity("train", "T")

    # The released blocks are driven to 0 on the next tick, and nothing a later
    # train needs is still held for the deleted one.
    sim.update(0.1)
    voltages = sim.power_controller.get_all_voltages()
    assert all(not voltages.get(block_id) for block_id in held)


def test_a_later_train_with_the_same_id_starts_fresh():
    sim, state = _line_sim()
    _train, old_controller = _place(sim)
    _route(sim, "R", "BLK_6")
    assert sim.route_executor.request_route_assignment("T", "R").success
    _run_then_power_off(sim, old_controller)
    assert sim.route_executor.request_route_assignment("T", "R").success

    train_service.delete_train(state, "T")
    _new_train, new_controller = _place(sim)

    assert new_controller is not old_controller
    assert new_controller.train_status == TrainStatus.OFF
    assert new_controller.commands.engaged is False
    assert sim.route_executor.assigned_route_id_for("T") is None


def test_an_emptied_one_off_route_and_a_dropped_one_off_request_are_deleted():
    sim, state = _line_sim()
    _place(sim)
    rex = sim.route_executor
    one_off = _route(sim, "QUICK", "BLK_6", mode=RouteMode.ONE_OFF)
    held = _route(sim, "HELD", "BLK_5", mode=RouteMode.ONE_OFF)
    assert rex.request_route_assignment("T", "QUICK").success
    # A request held for a teardown that will never run once the train is gone.
    rex._hold_route_assignment("T", "HELD", None)

    train_service.delete_train(state, "T")

    assert "QUICK" not in rex.route_registry
    assert rex.get_historical_route("QUICK") is one_off
    assert "HELD" not in rex.route_registry
    assert rex.get_historical_route("HELD") is held
    assert "T" not in rex._pending_route_assignments


def test_switch_occupancy_and_protection_are_released():
    sim, state = _line_sim()
    sim.network.trains["T"] = Train(train_id="T")
    switch = Switch(switch_id="SW_1", x=20, y=20)
    neighbour = Switch(switch_id="SW_2", x=21, y=20)
    route = frozenset({"W", "E"})
    switch.route_owners[route] = "T"
    neighbour.train_protection_claims.append(("T", "SW_1", 0))
    sim.network.switches["SW_1"] = switch
    sim.network.switches["SW_2"] = neighbour

    train_service.delete_train(state, "T")

    assert "T" not in switch.route_owners.values()
    assert neighbour.train_protection_claims == []


def test_the_rest_endpoint_reports_a_refusal_and_deletes_an_off_train():
    from app_factory import create_app

    sim, state = _line_sim()
    _train, controller = _place(sim)
    client = create_app(state).test_client()

    assert controller.commands.set_mode("manual").success
    refused = client.delete("/api/trains/T")
    assert refused.status_code == 400
    assert "off" in refused.get_json()["error"]

    assert controller.commands.power_off_cmd().success
    sim.update(0.1)
    accepted = client.delete("/api/trains/T")
    assert accepted.status_code == 200
    assert "T" not in sim.network.trains
    assert client.delete("/api/trains/T").status_code == 404
