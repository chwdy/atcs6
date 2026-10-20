"""Lighting-voltage calibration: candidate stepping, bounds, and the commit.

design_new.md "Lighting-voltage calibration": the pass puts a candidate voltage
on the rails and the operator says whether the locomotive moved — there is no
measurement step, because at this level the drive is too weak to load a current
channel and first movement is a creep of a few millimetres. The candidate stays
below the profile's `max_voltage` and below the Hardware Setup's
`current_sense_min_drive_v`, and committing moves the 0%-power anchor of
`voltage_power_curve` with it.
"""

import pytest

from constants import LIGHTING_VOLTAGE_STEP_V
from enums import TrainStatus
from hardware_config import DEFAULT_CURRENT_SENSE_MIN_DRIVE_V
from speed_calibration import SpeedCalibration
from speed_profile import make_default_speed_profile
from train_controller_types import PHYSICS_KIND_HANDLE, PhysicsCommand
from train_physics import TrainPhysics


class _StubTrain:
    train_id = "T1"


def _open_run(profile=None):
    profile = profile or make_default_speed_profile("T1", 100.0)
    cal = SpeedCalibration()
    run = cal.start_lighting(_StubTrain(), profile)
    return cal, profile, run


# --------------------------------------------------------------- stepping ---


def test_run_starts_at_the_profiles_stored_lighting_voltage():
    cal, profile, run = _open_run()
    assert run.candidate_voltage == pytest.approx(profile.lighting_voltage)
    assert cal.lighting_candidate_voltage("T1") == pytest.approx(profile.lighting_voltage)


def test_each_step_moves_one_constant_step():
    cal, profile, _run = _open_run()
    start = profile.lighting_voltage
    cal.step_lighting("T1", 1)
    assert cal.lighting_candidate_voltage("T1") == pytest.approx(start + LIGHTING_VOLTAGE_STEP_V)
    cal.step_lighting("T1", -1)
    assert cal.lighting_candidate_voltage("T1") == pytest.approx(start)


def test_step_up_stops_at_the_ceiling_and_down_stops_at_zero():
    """The panel can never show a value the commit would refuse."""
    cal, profile, run = _open_run()
    for _ in range(500):
        cal.step_lighting("T1", 1)
    assert cal.lighting_candidate_voltage("T1") == pytest.approx(run.ceiling_voltage)
    for _ in range(500):
        cal.step_lighting("T1", -1)
    assert cal.lighting_candidate_voltage("T1") == 0.0


def test_ceiling_sits_below_the_current_sense_drive_level():
    """`current_sense_min_drive_v` must stay above `lighting_voltage`, so a lit
    but motionless train lands in the lighting zone rather than reading as
    driven (design_new.md "Power", Occupancy source follows drive state)."""
    profile = make_default_speed_profile("T1", 100.0)
    ceiling = SpeedCalibration.lighting_ceiling(profile)
    assert ceiling < DEFAULT_CURRENT_SENSE_MIN_DRIVE_V
    assert ceiling < profile.max_voltage


def test_ceiling_follows_max_voltage_when_it_is_the_lower_bound():
    profile = make_default_speed_profile("T1", 100.0)
    profile.max_voltage = 2.0
    assert SpeedCalibration.lighting_ceiling(profile) == pytest.approx(
        2.0 - LIGHTING_VOLTAGE_STEP_V
    )


def test_ceiling_ignores_current_sense_bound_in_simulation():
    """In Simulation the current_sense_min_drive_v bound is not evaluated
    (design_new.md:3197, "Simulation is independent of Hardware Setup"); the
    candidate is bounded only by max_voltage, so a Simulation ceiling is not
    over-capped by a Hardware-Setup value."""
    profile = make_default_speed_profile("T1", 100.0)
    # The default profile's max_voltage sits above current_sense_min_drive_v, so
    # the hardware bound is normally the lower one.
    hw_ceiling = SpeedCalibration.lighting_ceiling(profile, is_simulation=False)
    sim_ceiling = SpeedCalibration.lighting_ceiling(profile, is_simulation=True)
    assert sim_ceiling == pytest.approx(profile.max_voltage - LIGHTING_VOLTAGE_STEP_V)
    assert sim_ceiling > hw_ceiling


def test_stepping_without_an_open_run_is_a_no_op():
    cal = SpeedCalibration()
    assert cal.step_lighting("T1", 1) is None
    assert cal.lighting_candidate_voltage("T1") is None


# ----------------------------------------------------------------- commit ---


def test_commit_writes_the_candidate_and_closes_the_run():
    cal, profile, _run = _open_run()
    cal.step_lighting("T1", 1)
    candidate = cal.lighting_candidate_voltage("T1")
    assert cal.commit_lighting("T1", profile) == pytest.approx(candidate)
    assert profile.lighting_voltage == pytest.approx(candidate)
    assert cal.lighting_candidate_voltage("T1") is None


def test_commit_moves_the_curves_zero_power_anchor_up():
    """Points at or below the new anchor name voltages that do not move this
    locomotive, so they are dropped."""
    profile = make_default_speed_profile("T1", 100.0)
    profile.voltage_power_curve = [(1.5, 0.0), (2.0, 20.0), (3.0, 50.0), (12.0, 100.0)]
    cal, profile, _run = _open_run(profile)
    for _ in range(10):  # 1.5 -> 2.5
        cal.step_lighting("T1", 1)
    anchor = cal.commit_lighting("T1", profile)
    assert anchor == pytest.approx(2.5)
    assert profile.voltage_power_curve == [(2.5, 0.0), (3.0, 50.0), (12.0, 100.0)]


def test_commit_moving_the_anchor_down_leaves_exactly_one_zero_power_point():
    """The old anchor sits above the new one, so dropping only points at or
    below the anchor would leave the curve with two 0%-power points."""
    profile = make_default_speed_profile("T1", 100.0)
    profile.voltage_power_curve = [(1.5, 0.0), (2.0, 20.0), (12.0, 100.0)]
    cal, profile, _run = _open_run(profile)
    for _ in range(5):  # 1.5 -> 1.0
        cal.step_lighting("T1", -1)
    anchor = cal.commit_lighting("T1", profile)
    zero_points = [pt for pt in profile.voltage_power_curve if pt[1] == 0.0]
    assert zero_points == [(anchor, 0.0)]
    assert profile.voltage_power_curve == [(1.0, 0.0), (2.0, 20.0), (12.0, 100.0)]


def test_committed_curve_still_round_trips_both_anchors():
    cal, profile, _run = _open_run()
    for _ in range(6):
        cal.step_lighting("T1", 1)
    anchor = cal.commit_lighting("T1", profile)
    assert profile.voltage_to_power(anchor) == pytest.approx(0.0)
    assert profile.voltage_to_power(profile.max_voltage) == pytest.approx(100.0)


def test_cancel_discards_the_candidate():
    cal, profile, _run = _open_run()
    before = profile.lighting_voltage
    cal.step_lighting("T1", 1)
    cal.cancel_lighting("T1")
    assert cal.lighting_candidate_voltage("T1") is None
    assert profile.lighting_voltage == pytest.approx(before)


# ---------------------------------------------- the shared drive accessor ---


def test_get_propulsion_override_returns_the_lighting_track_voltage_arm():
    """The candidate leaves `SpeedCalibration` through the shared
    `get_propulsion_override(...)` accessor as the `track_voltage` variant of the
    closed union (design_new.md "Lighting-voltage calibration", Where it lives).
    This is the one channel the controller reads; physics no longer reaches into
    the run registry, so this replaces the old `compute_track_voltage` registry
    read as the assertion that a run drives its candidate."""
    cal, _profile, _run = _open_run()
    for _ in range(4):
        cal.step_lighting("T1", 1)
    candidate = cal.lighting_candidate_voltage("T1")

    override = cal.get_propulsion_override("T1")
    assert override is not None
    assert override.kind == "track_voltage"
    assert override.value == pytest.approx(candidate)


def test_no_override_once_the_run_is_closed():
    """Closing the run withdraws the override, so the controller falls back to
    its ordinary voltage source (design_new.md "Lighting-voltage calibration",
    Save and discard are the two operator-driven endings)."""
    cal, _profile, _run = _open_run()
    cal.step_lighting("T1", 1)
    cal.cancel_lighting("T1")
    assert cal.get_propulsion_override("T1") is None


# ---------------------------------------------------------------- physics ---
# Physics is an executor: it emits whatever voltage magnitude the controller
# hands down on `PhysicsCommand.voltage_source_track_voltage`, and never reads
# the calibration run registry itself (design_new.md "Lighting-voltage
# calibration", Where it lives). These drive `compute_track_voltage` with a
# hand-built command so each branch of its early voltage flow is testable on its
# own; the real controller-published command is covered end-to-end by
# `test_open_run_drives_its_candidate_through_the_real_controller` below.


class _StubEB:
    active = False


class _StubPathProjection:
    received_path_plan = None


class _StubController:
    """Enough of a controller for `compute_track_voltage`: the stored `powered`
    flag, the stopped/reservation/EB reads the voltage-shape branch consults,
    and the per-tick `PhysicsCommand` producer physics reads the voltage source
    off of. Powered + stopped + no reservation is the stopped-but-lit case."""

    def __init__(self, command):
        self.powered = True
        self.eb = _StubEB()
        self.path_projection = _StubPathProjection()
        self._command = command

    def is_stopped(self):
        return True

    def produce_physics_command(self):
        return self._command


def test_physics_emits_the_commands_track_voltage_in_place_of_the_lit_voltage():
    """When the command carries a `voltage_source_track_voltage`, physics emits
    exactly that magnitude in place of the stopped-but-lit value the READY state
    would otherwise output."""
    profile = make_default_speed_profile("T1", 100.0)
    candidate = profile.lighting_voltage + 4 * LIGHTING_VOLTAGE_STEP_V
    assert candidate != pytest.approx(profile.lighting_voltage)
    physics = TrainPhysics(train_id="T1")
    controller = _StubController(
        PhysicsCommand(
            kind=PHYSICS_KIND_HANDLE, voltage_source_track_voltage=candidate
        )
    )
    assert physics.compute_track_voltage(controller, profile) == pytest.approx(candidate)


def test_physics_returns_the_stored_lit_voltage_when_no_override_is_on_the_command():
    """With no track-voltage override on the command, a READY train emits its
    stored lighting voltage — the ordinary path is untouched by the migration."""
    profile = make_default_speed_profile("T1", 100.0)
    physics = TrainPhysics(train_id="T1")
    controller = _StubController(PhysicsCommand(kind=PHYSICS_KIND_HANDLE))
    assert physics.compute_track_voltage(controller, profile) == pytest.approx(
        profile.lighting_voltage
    )


def test_a_powered_off_train_stays_dead_even_with_a_track_voltage_override():
    """OFF outputs 0 V before the command is even read, so a lingering override
    can never light a powered-off train."""
    profile = make_default_speed_profile("T1", 100.0)
    physics = TrainPhysics(train_id="T1")
    controller = _StubController(
        PhysicsCommand(kind=PHYSICS_KIND_HANDLE, voltage_source_track_voltage=3.0)
    )
    controller.powered = False
    assert physics.compute_track_voltage(controller, profile) == 0.0


class _Entry:
    def __init__(self, resource_id):
        self.resource_id = resource_id


class _OverrunPlan:
    def __init__(self, route):
        self.resource_sequence = [_Entry(r) for r in route]


class _OverrunProjection:
    def __init__(self, plan):
        self.received_path_plan = plan


class _OverrunController:
    """A powered, rolling train with an authorized route and a head tile, for the
    overrun voltage branch of `compute_track_voltage`."""

    def __init__(self, head_tile, route):
        self.powered = True
        self.eb = _StubEB()
        self.path_projection = _OverrunProjection(_OverrunPlan(route))
        self._head_tile = head_tile

    def is_stopped(self):
        return False

    def _get_head_tile(self):
        return self._head_tile

    def produce_physics_command(self):
        return PhysicsCommand(kind=PHYSICS_KIND_HANDLE, voltage_source_speed_kmh=20.0)


def test_a_train_on_its_authorized_path_drives_normally():
    """A head on a block that is on its authorized route is normal travel, so it
    drives the speed-derived voltage.

    An overrun no longer has a test of its own in `compute_track_voltage`: when a
    train runs its head onto a block not reserved for it, Observe raises the
    train's emergency cut-out, which drives the whole train to 0 V through the
    Propulsion cap step (design_new.md "Emergency cut-out"). That end-to-end
    cut-out is exercised by
    tests/test_overrun_past_authority_journey.py."""
    from tests.test_block_occupancy_cap import build_line

    network = build_line()
    profile = make_default_speed_profile("T", 100.0)
    physics = TrainPhysics(train_id="T", network=network)

    normal = _OverrunController((1, 0), ["BLK_0", "BLK_1"])
    assert physics.compute_track_voltage(normal, profile) > profile.lighting_voltage


# --------------------------------------------------- service-layer gates ----


@pytest.fixture
def state_with_train():
    """AppState with one placed train on its own SpeedProfile."""
    import threading
    from datetime import datetime

    from app_state import AppState
    from network import RailwayNetwork
    from path_dispatcher import PathDispatcher
    from persistence import PersistenceManager
    from enums import Direction
    from simulation import SimulationController
    from tests.segment_test_helpers import make_segment_block_with_tracks
    from tests.train_test_helpers import set_train_span
    from track import TRACK_EW, Tile
    from train import Train, TrainCar

    network = RailwayNetwork()
    dispatcher = PathDispatcher(network)
    sim = SimulationController(network, dispatcher)
    state = AppState(
        network=network,
        path_dispatcher=dispatcher,
        sim_controller=sim,
        persistence_manager=PersistenceManager(storage_dir="layouts"),
        simulation_lock=threading.Lock(),
        # A calibration run opens only while the session runs.
        simulation_running=True,
        simulation_speed=1.0,
        last_update_time=datetime.now(),
    )
    profile = make_default_speed_profile("PROF_A", 80.0)
    network.speed_profiles.register(profile)
    for x in range(2):
        network.add_track(Tile(x, 0, track_bits=TRACK_EW))
    make_segment_block_with_tracks(
        network, "BLK_1", positions=[(0, 0), (1, 0)], name="1", length_cm=200.0
    )
    train = Train(
        train_id="TRAIN_A",
        consist=[TrainCar(car_id="CAR_1", car_type="LOCOMOTIVE", length_cm=20.0)],
        max_speed=80.0,
    )
    train.speed_profile_id = profile.profile_id
    network.add_train(train)
    network.place_train(train, 0, 0, direction=Direction.EAST)
    controller = sim._get_or_create_controller(train)
    set_train_span(
        controller, (0, 0), facing_direction=Direction.EAST, occupied_blocks=["BLK_1"]
    )
    return state


def _ready_manual_with_cab(state):
    """Bring the fixture train to the one state a run may open from."""
    from services import train_service

    controller = state.sim_controller._get_or_create_controller(
        state.network.trains["TRAIN_A"]
    )
    controller.commands.set_mode("manual")
    controller.commands.power_on_cmd()
    controller._apply_head_frame("end_a")
    assert controller.train_status == TrainStatus.MANUAL and controller.is_stopped()
    assert controller._framed_head_end == "end_a"
    return train_service, controller


def test_service_opens_a_run_from_ready_manual_with_a_cab(state_with_train):
    train_service, _controller = _ready_manual_with_cab(state_with_train)
    run = train_service.start_lighting_calibration(state_with_train, "TRAIN_A")
    assert run.train_id == "TRAIN_A"
    assert (
        state_with_train.sim_controller.speed_calibration.lighting_candidate_voltage("TRAIN_A")
        is not None
    )


def test_opening_takes_control_of_an_ato_train(state_with_train):
    """Opening a run takes control of the train (design_new.md "Speed
    calibration", Opening a run takes control of the train): a stopped ATO train
    with no route is switched to manual and reads CALIBRATING."""
    train_service, controller = _ready_manual_with_cab(state_with_train)
    controller.commands.set_mode("ato")

    train_service.start_lighting_calibration(state_with_train, "TRAIN_A")

    assert controller.commands.control_mode == "manual"
    assert controller.train_status == TrainStatus.CALIBRATING


def test_opening_from_off_powers_the_train_up(state_with_train):
    from services import train_service

    controller = state_with_train.sim_controller.controllers["TRAIN_A"]
    assert controller.powered is False

    train_service.start_lighting_calibration(state_with_train, "TRAIN_A")

    assert controller.powered is True
    assert controller.train_status == TrainStatus.CALIBRATING


def test_opening_is_refused_while_the_session_is_stopped(state_with_train):
    from services import ConflictError

    train_service, _controller = _ready_manual_with_cab(state_with_train)
    state_with_train.simulation_running = False

    with pytest.raises(ConflictError, match="not live"):
        train_service.start_lighting_calibration(state_with_train, "TRAIN_A")


def test_service_rejects_a_run_while_a_speed_pass_is_open(state_with_train):
    from services import ConflictError

    train_service, _controller = _ready_manual_with_cab(state_with_train)
    calibration = state_with_train.sim_controller.speed_calibration
    profile = state_with_train.network.speed_profiles.get(
        state_with_train.network.trains["TRAIN_A"].speed_profile_id
    )
    calibration.start(state_with_train.network.trains["TRAIN_A"], profile)

    with pytest.raises(ConflictError, match="speed-calibration"):
        train_service.start_lighting_calibration(state_with_train, "TRAIN_A")


def test_step_and_save_need_an_open_run(state_with_train):
    from services import ConflictError

    train_service, _controller = _ready_manual_with_cab(state_with_train)
    with pytest.raises(ConflictError, match="No lighting-voltage run"):
        train_service.step_lighting_calibration(state_with_train, "TRAIN_A", 1)
    with pytest.raises(ConflictError, match="No lighting-voltage run"):
        train_service.save_lighting_calibration(state_with_train, "TRAIN_A")


def test_service_save_writes_the_profile_and_closes_the_run(state_with_train):
    train_service, _controller = _ready_manual_with_cab(state_with_train)
    train_service.start_lighting_calibration(state_with_train, "TRAIN_A")
    train_service.step_lighting_calibration(state_with_train, "TRAIN_A", 1)

    saved = train_service.save_lighting_calibration(state_with_train, "TRAIN_A")
    profile = state_with_train.network.speed_profiles.get(
        state_with_train.network.trains["TRAIN_A"].speed_profile_id
    )
    assert profile.lighting_voltage == pytest.approx(saved)
    assert (
        state_with_train.sim_controller.speed_calibration.lighting_candidate_voltage("TRAIN_A")
        is None
    )


def test_open_run_drives_its_candidate_through_the_real_controller(state_with_train):
    """End-to-end: an open run makes the real `TrainController.produce_physics_command`
    publish the candidate on `voltage_source_track_voltage`, carrying no motion,
    and `TrainPhysics.compute_track_voltage` emits exactly that magnitude — the
    whole canonical drive path, with no registry read anywhere in it (design_new.md
    "Lighting-voltage calibration", What the run drives / Where it lives)."""
    train_service, controller = _ready_manual_with_cab(state_with_train)
    sim = state_with_train.sim_controller
    train = state_with_train.network.trains["TRAIN_A"]
    profile = state_with_train.network.speed_profiles.get(train.speed_profile_id)

    train_service.start_lighting_calibration(state_with_train, "TRAIN_A")
    for _ in range(3):
        train_service.step_lighting_calibration(state_with_train, "TRAIN_A", 1)
    candidate = sim.speed_calibration.lighting_candidate_voltage("TRAIN_A")

    command = controller.produce_physics_command()
    assert command.voltage_source_track_voltage == pytest.approx(candidate)
    # The candidate never feeds the speed solution, so the command commands no
    # motion: it holds the ordinary handle-N traction while overriding voltage.
    assert command.target_speed_kmh == 0.0

    physics = sim._get_or_create_physics(train)
    assert physics.compute_track_voltage(controller, profile) == pytest.approx(candidate)


def test_a_route_assignment_is_refused_while_the_run_is_open(state_with_train):
    """A calibrating train refuses route assignment (design_new.md "Route
    assignment writers", A calibrating train refuses assignment); once the run
    ends the controller publishes no track-voltage override, so voltage returns
    to the stored value."""
    train_service, controller = _ready_manual_with_cab(state_with_train)
    sim = state_with_train.sim_controller
    calibration = sim.speed_calibration

    train_service.start_lighting_calibration(state_with_train, "TRAIN_A")
    assert calibration.get_propulsion_override("TRAIN_A") is not None

    result = sim.route_executor.request_route_assignment("TRAIN_A", None)
    assert result.success is False
    assert "lighting-voltage run" in result.message

    calibration.cancel_lighting("TRAIN_A")
    assert calibration.get_propulsion_override("TRAIN_A") is None
    assert controller.produce_physics_command().voltage_source_track_voltage is None


def test_the_status_endpoint_reports_an_open_run_and_its_absence(state_with_train):
    """The panel reads the open run when the operator names a train, so a run
    left open by a page reload shows on its row and can be ended."""
    from app_factory import create_app

    train_service, _controller = _ready_manual_with_cab(state_with_train)
    app = create_app(state_with_train)
    app.config["TESTING"] = True
    with app.test_client() as client:
        assert client.get("/api/trains/TRAIN_A/calibrate/lighting").get_json() == {
            "open": False
        }

        train_service.start_lighting_calibration(state_with_train, "TRAIN_A")
        train_service.step_lighting_calibration(state_with_train, "TRAIN_A", 1)
        body = client.get("/api/trains/TRAIN_A/calibrate/lighting").get_json()

    run = state_with_train.sim_controller.speed_calibration.lighting_runs["TRAIN_A"]
    assert body["open"] is True
    assert body["candidate_voltage"] == pytest.approx(run.candidate_voltage)
    assert body["ceiling_voltage"] == pytest.approx(run.ceiling_voltage)
    assert body["step_v"] == pytest.approx(LIGHTING_VOLTAGE_STEP_V)
