"""A train with an open calibration run reads CALIBRATING and refuses driver input.

design_new.md "Controller state machine": `train_status` is derived in the order
OFF > EB > CALIBRATING > control_mode, and CALIBRATING holds while a calibration
run is open for the train. While it holds, the run drives the train in every
submode, and set_mode, set_manual_mode_type, ATO go, and driver input are
refused; the operator EB is still accepted.
"""

from types import SimpleNamespace

from enums import Direction, HandlePosition, TrainStatus
from network import RailwayNetwork
from speed_calibration import CalibrationPropulsionOverride
from train import Train, TrainCar
from train_controller import create_controller_for_train
from train_controller_types import PHYSICS_KIND_TARGET_SPEED
from tests.segment_test_helpers import make_segment_block_with_tracks
from tests.train_test_helpers import set_train_span


class _OpenRun:
    """Stands in for `SpeedCalibration`: one run open for the train it names."""

    def __init__(self, train_id, override):
        self.train_id = train_id
        self.override = override

    def get_propulsion_override(self, train_id):
        return self.override if train_id == self.train_id else None

    def taint_current_crossing(self, train_id, reason):
        # No crossing is being timed in these tests, so nothing is marked.
        return False


def _make_controller(override=None):
    from track import TRACK_EW, Tile

    network = RailwayNetwork()
    for x in range(2):
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
    controller.commands.control_mode = "manual"
    if override is not None:
        controller.wire_dependencies(speed_calibration=_OpenRun("T1", override))
    return controller


_SPEED_PASS = CalibrationPropulsionOverride(track_voltage=4.8)


def test_no_open_run_reads_the_control_mode():
    controller = _make_controller()
    assert controller.calibration_run_open() is False
    assert controller.ops.train_status == TrainStatus.MANUAL


def test_an_open_speed_pass_reads_calibrating():
    controller = _make_controller(_SPEED_PASS)
    assert controller.ops.train_status == TrainStatus.CALIBRATING


def test_a_pass_driving_zero_still_reads_calibrating():
    """A pass between legs drives `0`, and still owns the train."""
    controller = _make_controller(CalibrationPropulsionOverride(track_voltage=0.0))
    assert controller.ops.train_status == TrainStatus.CALIBRATING


def test_off_and_eb_outrank_calibrating():
    controller = _make_controller(_SPEED_PASS)
    assert controller.eb.activate().success
    assert controller.ops.train_status == TrainStatus.EB
    controller.powered = False
    assert controller.ops.train_status == TrainStatus.OFF


def test_the_run_drives_outside_the_power_percent_submode():
    """The run's voltage is the drive source whatever the submode is: in the
    `simulation` submode the controller would otherwise send the handle notch."""
    controller = _make_controller(_SPEED_PASS)
    controller.manual.manual_mode_type = "simulation"
    controller.manual.manual_power_percent = 0

    cmd = controller.produce_physics_command()

    assert cmd.kind == PHYSICS_KIND_TARGET_SPEED
    # The run's drive is never copied into the stored manual percent.
    assert controller.manual.manual_power_percent == 0


def test_mode_and_submode_changes_are_refused_while_calibrating():
    controller = _make_controller(_SPEED_PASS)

    assert controller.commands.set_mode("ato").success is False
    assert controller.commands.control_mode == "manual"
    assert controller.commands.set_manual_mode_type("simulation").success is False


def test_driver_input_is_refused_while_calibrating():
    controller = _make_controller(_SPEED_PASS)
    controller.manual.manual_mode_type = "power_percent"
    before = controller.manual.manual_power_percent

    assert controller.commands.set_manual_power_percent(60).success is False
    assert controller.manual.manual_power_percent == before
    controller.manual.manual_mode_type = "simulation"
    assert controller.commands.set_handle(HandlePosition.P3).success is False


def test_ato_go_is_refused_while_calibrating():
    controller = _make_controller(_SPEED_PASS)
    controller.commands.control_mode = "ato"
    assert controller.commands.ato_go().success is False
    assert controller.commands.engaged is False


def test_the_operator_emergency_brake_is_accepted_while_calibrating():
    controller = _make_controller(_SPEED_PASS)
    assert controller.eb.activate().success
    assert controller.eb.active is True
