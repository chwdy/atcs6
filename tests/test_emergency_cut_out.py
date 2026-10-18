"""Emergency cut-out (design_new.md "Emergency cut-out").

The emergency cut-out is the emergency-brake latch with ``eb.cut_out`` set: it
drives the train's traction voltage to ``0`` and stops it the same tick in every
submode, instead of braking down the ``emergency_brake_rate`` curve. These tests
lock in the controller-level behaviour (latch effects, the Propulsion cap-step
de-energize, serialization), the operator env gate, and the layout-wide command.
The Observe overrun trigger is exercised end-to-end in
``tests/test_overrun_past_authority_journey.py``.
"""

import pytest

import operation_environment
from enums import Direction, HandlePosition, OperationMode
from network import RailwayNetwork
from path_dispatcher import PathDispatcher
from simulation import SimulationController
from train import Train, TrainCar
from train_controller import create_controller_for_train
from train_controller.serialization import controller_from_dict, controller_to_dict
from train_controller_types import PHYSICS_KIND_TARGET_SPEED
from tests.segment_test_helpers import make_segment_block_with_tracks
from tests.train_test_helpers import set_train_span


def _make_powered_controller():
    network = RailwayNetwork()
    for x in range(2):
        from track import Tile, TRACK_EW

        network.add_track(Tile(x, 0, track_bits=TRACK_EW))
    make_segment_block_with_tracks(
        network, "BLK_1", positions=[(0, 0), (1, 0)], name="1", length_cm=200.0
    )
    car = TrainCar(car_id="C1", car_type="LOCOMOTIVE", length_cm=40.0)
    train = Train(train_id="T1", consist=[car], max_speed=80.0)
    network.add_train(train)
    network.place_train(train, 0, 0, direction=Direction.EAST)

    controller = create_controller_for_train(train.train_id, network)
    set_train_span(
        controller, (0, 0), facing_direction=Direction.EAST, occupied_blocks=["BLK_1"]
    )
    controller.powered = True
    return network, controller


def test_activate_cut_out_sets_latch_and_canonical_effects():
    _network, controller = _make_powered_controller()
    controller.commands.control_mode = "ato"
    controller.commands.engaged = True

    result = controller.eb.activate_cut_out()

    assert result.success
    assert controller.eb.active is True
    assert controller.eb.cut_out is True
    # Shares every emergency-brake effect (design_new.md "Emergency cut-out").
    assert controller.ops.handle_position == HandlePosition.EB
    # EB keeps control_mode and clears the engage bit.
    assert controller.commands.control_mode == "ato"
    assert controller.commands.engaged is False


def test_activate_cut_out_no_ops_when_already_latched():
    """A cut-out on an already-latched train is a no-op on the ``active == False``
    gate; a plain brake already latched stays a plain brake."""
    _network, controller = _make_powered_controller()

    assert controller.eb.activate().success
    assert controller.eb.active is True
    assert controller.eb.cut_out is False

    result = controller.eb.activate_cut_out()
    assert result.success is False
    # The already-latched plain brake is not upgraded to a cut-out.
    assert controller.eb.cut_out is False


def test_release_clears_cut_out():
    _network, controller = _make_powered_controller()
    controller.eb.activate_cut_out()
    assert controller.eb.cut_out is True

    # Stopped, no live path -> the four release conditions hold.
    controller.physics_sync.current_speed_kmh = 0.0
    result = controller.eb.release()

    assert result.success
    assert controller.eb.active is False
    assert controller.eb.cut_out is False


@pytest.mark.parametrize(
    "control_mode,manual_mode_type",
    [("ato", "simulation"), ("manual", "simulation"), ("manual", "power_percent")],
)
def test_cap_step_cut_out_de_energizes_in_every_submode(control_mode, manual_mode_type):
    """The cut-out ceiling is a momentum-free zero applied THIS tick in every
    submode, with the emitted ``track_voltage`` driven to ``0`` (design_new.md
    "Emergency cut-out"; EB workflow state machine row ``e67d``)."""
    _network, controller = _make_powered_controller()
    controller.eb.activate_cut_out()
    # Set the submode directly to prove the cut-out branch is submode-independent
    # (activation leaves control_mode as it was).
    controller.commands.control_mode = control_mode
    controller.manual.manual_mode_type = manual_mode_type

    cmd = controller.produce_physics_command()

    assert cmd.kind == PHYSICS_KIND_TARGET_SPEED
    assert cmd.target_speed_kmh == 0.0
    # Fully de-energized, not the stopped-but-lit lighting voltage a plain zero
    # ceiling emits.
    assert cmd.voltage_source_track_voltage == 0.0


def test_serialization_round_trips_cut_out():
    network, controller = _make_powered_controller()
    controller.eb.activate_cut_out()

    data = controller_to_dict(controller)
    assert data["eb_cut_out"] is True

    restored = controller_from_dict(data, network)
    assert restored.eb.active is True
    assert restored.eb.cut_out is True


def test_operator_cut_out_is_gated_to_hardware(monkeypatch):
    _network, controller = _make_powered_controller()

    # Simulation: the operator-initiated cut-out is not offered.
    monkeypatch.setattr(operation_environment, "_selected", OperationMode.SIMULATION)
    result = controller.commands.activate_cut_out_cmd()
    assert result.success is False
    assert controller.eb.cut_out is False

    # Virtual Hardware: accepted.
    monkeypatch.setattr(
        operation_environment, "_selected", OperationMode.VIRTUAL_HARDWARE
    )
    result = controller.commands.activate_cut_out_cmd()
    assert result.success
    assert controller.eb.active is True
    assert controller.eb.cut_out is True


def _make_sim_with_train():
    network = RailwayNetwork()
    from track import Tile, TRACK_EW

    for x in range(2):
        network.add_track(Tile(x, 0, track_bits=TRACK_EW))
    make_segment_block_with_tracks(
        network, "BLK_1", positions=[(0, 0), (1, 0)], name="1", length_cm=200.0
    )
    car = TrainCar(car_id="C1", car_type="LOCOMOTIVE", length_cm=40.0)
    train = Train(train_id="T1", consist=[car], max_speed=80.0)
    network.add_train(train)
    network.place_train(train, 0, 0, direction=Direction.EAST)
    dispatcher = PathDispatcher(network)
    sim = SimulationController(network, dispatcher)
    controller = sim._get_or_create_controller(train)
    controller.powered = True
    return sim, controller


def test_layout_emergency_cut_out_is_gated_and_cuts_every_train(monkeypatch):
    sim, controller = _make_sim_with_train()

    # Simulation: not offered; nothing is cut out.
    monkeypatch.setattr(operation_environment, "_selected", OperationMode.SIMULATION)
    sim.operation_mode = OperationMode.SIMULATION
    result = sim.layout_emergency_cut_out()
    assert result.success is False
    assert controller.eb.cut_out is False

    assert controller.eb.active is False

    # Virtual Hardware: the cut-out is called on every train between ticks
    # (design_new.md "Emergency cut-out", Layout emergency cut-out).
    monkeypatch.setattr(
        operation_environment, "_selected", OperationMode.VIRTUAL_HARDWARE
    )
    sim.operation_mode = OperationMode.VIRTUAL_HARDWARE
    result = sim.layout_emergency_cut_out()
    assert result.success
    assert controller.eb.active is True
    assert controller.eb.cut_out is True


def _make_app_client(tmp_path):
    import threading
    from datetime import datetime

    from app_factory import create_app
    from app_state import AppState
    from persistence import PersistenceManager

    sim, controller = _make_sim_with_train()
    network = sim.network
    state = AppState(
        network=network,
        path_dispatcher=sim.path_dispatcher,
        sim_controller=sim,
        persistence_manager=PersistenceManager(storage_dir=str(tmp_path)),
        simulation_lock=threading.Lock(),
        simulation_running=False,
        simulation_speed=1.0,
        last_update_time=datetime.now(),
    )
    app = create_app(state)
    app.config["TESTING"] = True
    return app.test_client(), controller, sim


def test_cut_out_endpoints_reject_in_simulation(tmp_path, monkeypatch):
    monkeypatch.setattr(operation_environment, "_selected", OperationMode.SIMULATION)
    client, controller, sim = _make_app_client(tmp_path)
    sim.operation_mode = OperationMode.SIMULATION

    assert client.post("/api/trains/T1/activate-cut-out").status_code == 400
    assert client.post("/api/simulation/emergency-cut-out").status_code == 400
    assert controller.eb.cut_out is False


def test_cut_out_endpoints_accept_on_hardware(tmp_path, monkeypatch):
    monkeypatch.setattr(
        operation_environment, "_selected", OperationMode.VIRTUAL_HARDWARE
    )
    client, controller, sim = _make_app_client(tmp_path)
    sim.operation_mode = OperationMode.VIRTUAL_HARDWARE

    resp = client.post("/api/trains/T1/activate-cut-out")
    assert resp.status_code == 200
    assert controller.eb.cut_out is True

    resp = client.post("/api/simulation/emergency-cut-out")
    assert resp.status_code == 200
