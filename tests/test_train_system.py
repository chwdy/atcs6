"""
Tests for Phase 1 - Train System Data Structures

This module tests:
- SpeedProfile: speed/voltage conversion, acceleration/deceleration
- Route & RouteTask: route traversal
- Train modifications: new properties, turn_around, state management
"""

import pytest
from speed_profile import SpeedProfile, DEFAULT_SPEED_PROFILE
from train_route import PlatformTarget, Route, RouteTask, RouteMode
from train import Train, TrainCar
from enums import Direction, TrainStatus, BlockState, HandlePosition
from network import RailwayNetwork
from train_controller import TrainController, create_controller_for_train
from runtime_payloads import PhysicsSpanUpdate
from tests.train_test_helpers import set_train_span


# =============================================================================
# Controller Test Helpers
# =============================================================================


def create_test_train_with_controller(
    train_id: str = "T001",
    powered: bool = False,
    current_speed: float = 0.0,
    max_speed: float = 100.0,
) -> tuple[Train, TrainController, RailwayNetwork]:
    """
    Create a train with a controller for testing.

    `powered` sets the stored power flag (design_new.md "Controller state
    machine"); `train_status` derives from it. `current_speed` seeds the live
    speed used by the derived stopped/moving predicates.

    Returns:
        Tuple of (train, controller, network)
    """
    network = RailwayNetwork()
    train = Train(
        train_id=train_id,
        max_speed=max_speed,
    )
    network.add_train(train)
    controller = create_controller_for_train(train_id, network)

    # Per design_new.md "Train ownership", live runtime state lives on the
    # controller; tests configure it directly there instead of mirroring
    # through Train.
    controller.powered = bool(powered)

    # Sync controller speed (canonical per design_new.md)
    controller.physics_sync.current_speed_kmh = float(current_speed)

    return train, controller, network


# =============================================================================
# SpeedProfile Tests
# =============================================================================


class TestSpeedProfile:
    """Tests for SpeedProfile class: the measured voltage/speed curve and the
    defined power percent (design_new.md "SpeedProfile")."""

    @pytest.fixture
    def basic_profile(self) -> SpeedProfile:
        """A measured curve whose lowest crossing is at 3 V, a steep rise to 6 V
        and a flat top from 9 V (the shape a real locomotive gives), with the
        locomotive starting to move at 2.5 V."""
        return SpeedProfile(
            profile_id="test_profile",
            name="Test Profile",
            lighting_voltage=2.0,
            starting_voltage=2.5,
            voltage_speed_curve=[(3.0, 30.0), (6.0, 90.0), (9.0, 120.0), (12.0, 120.0)],
            acceleration_rate=10.0,
            deceleration_rate=15.0,
            emergency_brake_rate=30.0,
        )

    def test_profile_initialization(self, basic_profile: SpeedProfile):
        """Test profile is created correctly."""
        assert basic_profile.profile_id == "test_profile"
        assert basic_profile.name == "Test Profile"
        assert basic_profile.lighting_voltage == 2.0
        assert basic_profile.acceleration_rate == 10.0
        assert basic_profile.deceleration_rate == 15.0
        assert basic_profile.emergency_brake_rate == 30.0

    def test_the_starting_voltage_is_stored_and_ato_runs_from_the_first_point(self, basic_profile):
        """design_new.md "SpeedProfile", Starting voltage; ATO minimum voltage
        and ATO minimum speed: unset, the ATO minimum is the curve's first point
        above the starting voltage."""
        assert basic_profile.starting_voltage == 2.5
        assert basic_profile.first_point_voltage == 3.0
        assert basic_profile.ato_minimum_voltage == 3.0
        assert basic_profile.ato_minimum_speed_kmh == 30.0
        assert basic_profile.top_speed_kmh == 120.0

    def test_voltage_to_speed_reads_the_curve_forward(self, basic_profile):
        """At or below the starting voltage the locomotive does not move, so it
        reads 0 km/h; from there a straight line runs to the first point, and
        above it the curve interpolates (design_new.md "SpeedProfile", The curve
        is read from the starting voltage up)."""
        assert basic_profile.voltage_to_speed(2.0) == 0.0
        assert basic_profile.voltage_to_speed(2.5) == 0.0
        assert basic_profile.voltage_to_speed(2.75) == pytest.approx(15.0)
        assert basic_profile.voltage_to_speed(3.0) == 30.0
        assert basic_profile.voltage_to_speed(4.5) == pytest.approx(60.0)
        assert basic_profile.voltage_to_speed(12.0) == 120.0

    def test_speed_to_voltage_reads_the_curve_backward(self, basic_profile):
        assert basic_profile.speed_to_voltage(15.0) == pytest.approx(2.75)
        assert basic_profile.speed_to_voltage(30.0) == 3.0
        assert basic_profile.speed_to_voltage(60.0) == pytest.approx(4.5)
        assert basic_profile.speed_to_voltage(90.0) == 6.0

    def test_a_flat_stretch_maps_to_its_lowest_voltage(self, basic_profile):
        """design_new.md "Speed to voltage reads the curve backward": where the
        curve runs flat, a speed maps to the lowest voltage of that stretch."""
        assert basic_profile.speed_to_voltage(120.0) == 9.0

    def test_a_speed_above_the_top_speed_maps_to_max_voltage(self, basic_profile):
        assert basic_profile.speed_to_voltage(150.0) == 12.0

    def test_speed_zero_is_the_lighting_voltage(self, basic_profile):
        """A speed of zero or below is what a powered train that nothing drives
        outputs: its `lighting_voltage` (design_new.md "TrainPhysics")."""
        assert basic_profile.speed_to_voltage(0.0) == 2.0
        assert basic_profile.speed_to_voltage(-10.0) == 2.0

    def test_power_percent_is_a_straight_line_in_speed(self, basic_profile):
        """design_new.md "Power percent is a share of the speed range": 0% is
        stopped, a percent p above 0 is top_speed x p / 100, and every step adds
        the same speed."""
        assert basic_profile.power_percent_to_speed(0.0) == 0.0
        assert basic_profile.power_percent_to_speed(0.001) == pytest.approx(0.0012)
        assert basic_profile.power_percent_to_speed(50.0) == pytest.approx(60.0)
        assert basic_profile.power_percent_to_speed(100.0) == 120.0
        assert basic_profile.speed_to_power_percent(60.0) == pytest.approx(50.0)
        assert basic_profile.speed_to_power_percent(0.0) == 0.0

    def test_power_percent_reaches_voltage_through_speed(self, basic_profile):
        # 50% is 60 km/h, which the curve reads backward as 4.5 V.
        assert basic_profile.power_percent_to_voltage(50.0) == pytest.approx(4.5)
        assert basic_profile.power_percent_to_voltage(0.0) == 2.0

    def test_a_percent_just_above_zero_puts_just_over_the_starting_voltage_on(self, basic_profile):
        """Manual driving runs from the starting voltage up (design_new.md
        "SpeedProfile", Starting voltage)."""
        volts = basic_profile.power_percent_to_voltage(0.5)
        assert 2.5 < volts < 2.6

    def test_a_stored_point_at_or_below_the_starting_voltage_is_not_read(self):
        profile = SpeedProfile(
            profile_id="p",
            name="p",
            starting_voltage=4.0,
            voltage_speed_curve=[(3.0, 30.0), (6.0, 90.0), (12.0, 120.0)],
        )
        assert profile.voltage_to_speed(3.5) == 0.0
        assert profile.voltage_to_speed(5.0) == pytest.approx(45.0)
        assert profile.first_point_voltage == 6.0

    def test_to_dict_from_dict_roundtrip(self, basic_profile: SpeedProfile):
        """Test serialization roundtrip."""
        data = basic_profile.to_dict()
        restored = SpeedProfile.from_dict(data)

        assert restored.profile_id == basic_profile.profile_id
        assert restored.name == basic_profile.name
        assert restored.lighting_voltage == basic_profile.lighting_voltage
        assert restored.acceleration_rate == basic_profile.acceleration_rate
        assert restored.voltage_speed_curve == basic_profile.voltage_speed_curve

    def test_default_speed_profile_exists(self):
        """DEFAULT_SPEED_PROFILE carries the seeded straight-line curve and no
        lighting voltage."""
        assert DEFAULT_SPEED_PROFILE is not None
        assert DEFAULT_SPEED_PROFILE.profile_id == "default"
        assert DEFAULT_SPEED_PROFILE.lighting_voltage is None
        assert DEFAULT_SPEED_PROFILE.top_speed_kmh == 100.0
        assert DEFAULT_SPEED_PROFILE.voltage_speed_curve[-1][0] == 12.0
        assert DEFAULT_SPEED_PROFILE.speed_to_voltage(60.0) > 0


# =============================================================================
# RouteTask Tests (v1; was TestWaypoint)
# =============================================================================


class TestRouteTask:
    """Tests for the RouteTask class (v1 replacement for Waypoint)."""

    def test_task_initialization(self):
        """Test task default values."""
        task = RouteTask(
            task_kind="dwell",
            task_location=PlatformTarget(platform_id="STN_001"),
            task_time=30.0,
        )
        assert task.task_location == PlatformTarget(platform_id="STN_001")
        assert task.task_kind == "dwell"
        assert task.task_time == 30.0
        assert task.target_time is None

    def test_should_depart_relative(self):
        """Test departure with task_time only."""
        task = RouteTask(
            task_kind="dwell",
            task_location=PlatformTarget(platform_id="STN_001"),
            task_time=30.0,
        )

        # Not enough time yet (dwell started at 0, now 25 = 25s elapsed < 30s)
        assert task.should_depart(0.0, 25.0, 0.0) is False

        # Enough time elapsed
        assert task.should_depart(0.0, 30.0, 0.0) is True
        assert task.should_depart(0.0, 35.0, 0.0) is True

    def test_should_depart_absolute_mode(self):
        """With `target_time` set the schedule names the moment, and it is read
        in wall time under either timing basis (design_new.md "Task time
        semantics")."""
        task = RouteTask(
            task_kind="dwell",
            task_location=PlatformTarget(platform_id="STN_001"),
            target_time=500.0,  # Leave at simulation time 500
        )

        # Before target time
        assert task.should_depart(450.0, 450.0, 400.0) is False

        # At target time
        assert task.should_depart(500.0, 500.0, 400.0) is True

        # After target time
        assert task.should_depart(550.0, 550.0, 400.0) is True

    def test_to_dict_from_dict_roundtrip(self):
        """Test task serialization."""
        task = RouteTask(
            task_kind="dwell",
            task_location=PlatformTarget(platform_id="STN_002"),
            task_time=45.0,
            target_time=120.0,
        )

        data = task.to_dict()
        restored = RouteTask.from_dict(data)

        assert restored.task_location == task.task_location
        assert restored.task_kind == task.task_kind
        assert restored.task_time == task.task_time
        assert restored.target_time == task.target_time


# =============================================================================
# Route Tests
# =============================================================================


class TestRoute:
    """Tests for Route class (v1: tasks instead of waypoints)."""

    @pytest.fixture
    def simple_route(self) -> Route:
        """Create a simple 3-task route."""
        return Route(
            route_id="ROUTE_001",
            name="Test Route",
            tasks=[
                RouteTask(task_kind="dwell", task_location="A"),
                RouteTask(task_kind="dwell", task_location="B"),
                RouteTask(task_kind="dwell", task_location="C"),
            ],
            mode=RouteMode.ONE_OFF,
        )

    def test_route_initialization(self, simple_route: Route):
        """Test route default values."""
        assert simple_route.route_id == "ROUTE_001"
        assert simple_route.name == "Test Route"
        assert len(simple_route.tasks) == 3
        assert simple_route.mode == RouteMode.ONE_OFF

    def test_to_dict_from_dict_roundtrip(self, simple_route: Route):
        """Test route serialization."""

        data = simple_route.to_dict()
        restored = Route.from_dict(data)

        assert restored.route_id == simple_route.route_id
        assert restored.name == simple_route.name
        assert len(restored.tasks) == len(simple_route.tasks)
        assert restored.mode == simple_route.mode


# =============================================================================
# Train Phase 1 Tests
# =============================================================================


class TestTrainPhase1:
    """Tests for Train class Phase 1 modifications."""

    @pytest.fixture
    def basic_train(self) -> tuple[Train, TrainController]:
        """Create a basic train + controller for testing.

        Live position is the controller's span (seeded here); facing direction
        is read back through the controller, not a Train-side field.
        """
        network = RailwayNetwork()
        train = Train(
            train_id="TRAIN_001",
            max_speed=100.0,
        )
        train.model = "BR 218"
        train.min_radius_cm = 40.0
        network.add_train(train)
        controller = create_controller_for_train(train.train_id, network)
        set_train_span(controller, (0, 0), facing_direction=Direction.EAST)
        controller._apply_head_frame("end_a")
        controller.powered = False
        return train, controller

    def test_new_properties_initialization(
        self, basic_train: tuple[Train, TrainController]
    ):
        """Test new Phase 1 properties (Train-side persistent fields only).

        Per design_new.md "Train ownership", `state` and `travel_direction` live
        on `TrainController`, not `Train`; this test checks the
        persistent/configuration fields that remain on Train plus the
        controller-derived facing direction.
        """
        train, controller = basic_train
        assert train.model == "BR 218"
        assert train.min_radius_cm == 40.0
        assert controller._facing_direction() == Direction.EAST
        assert train.speed_profile_id is None
        # `Train.assigned_route_id` is no longer a field; the canonical store is
        # `RouteExecutor.assigned_route_id_for(train_id)`.
        assert not hasattr(train, "assigned_route_id")





    def test_power_on_off(self):
        """Test power_on_cmd and power_off_cmd via TrainController.

        v1: power_off_cmd sets pending_power_off latch consumed in Action teardown.
        Acceptance is immediate; OFF transition happens after Action teardown.
        """
        train, controller, network = create_test_train_with_controller(
            powered=False
        )

        # Power on
        result = controller.commands.power_on_cmd()
        assert result.success is True
        assert controller.powered
        assert controller.train_status != TrainStatus.OFF

        # Can't power on again
        result = controller.commands.power_on_cmd()
        assert result.success is False

        # Power off — acceptance is immediate, OFF transition deferred.
        result = controller.commands.power_off_cmd()
        assert result.success is True
        assert controller.route_hooks.phase_latches.power_off is True

    def test_power_off_while_moving_fails(self):
        """Test power_off_cmd fails when train is moving."""
        train, controller, network = create_test_train_with_controller(
            powered=True, current_speed=50.0
        )

        result = controller.commands.power_off_cmd()
        assert result.success is False
        assert controller.powered and not controller.is_stopped()


    def test_from_dict_restores_new_properties(self):
        """Test from_dict restores Phase 1 properties."""
        data = {
            "id": "TRAIN_002",
            "max_speed": 120.0,
            "speed": 30.0,
            "model": "Shinkansen E5",
            "min_radius_cm": 50.0,
            "facing_direction": "WEST",
            "travel_direction": "forward",
            "state": "ready",
            "speed_profile_id": "profile_002",
        }

        train = Train.from_dict(data)

        assert train.train_id == "TRAIN_002"
        assert train.model == "Shinkansen E5"
        assert train.min_radius_cm == 50.0
        assert train.speed_profile_id == "profile_002"
        # `assigned_route_id` is no longer carried on `Train`; the canonical
        # store is `RouteExecutor.assigned_route_id_for(train_id)`.
        # Per design_new.md "Train ownership", Train no longer mirrors live
        # state/travel_direction/current_speed; those load on the
        # controller via add_train(...) / explicit controller commands.

    def test_from_dict_handles_missing_new_properties(self):
        """Test from_dict handles missing Phase 1 properties (backwards compat)."""
        data = {
            "id": "TRAIN_OLD",
            "max_speed": 80.0,
            "speed": 0.0,
        }

        train = Train.from_dict(data)

        # Should use defaults for the persistent fields.
        assert train.model == ""
        assert train.min_radius_cm == 30.0
        assert train.speed_profile_id is None




# =============================================================================
# Phase 2 Tests - Block Power Controller
# =============================================================================

from block_power import (
    BlockPowerController,
    SimulatedTrackDriver,
    TrackOutput,
)
from block import Block
from train_controller import (
    calculate_max_safe_speed,
    TRACK_END_SAFETY_MARGIN_CM,
)
from block_power import MAX_TRACK_VOLTAGE


def _make_fake_bp_controller(powered, has_reservation=False, target_speed=0.0):
    """A stand-in controller for `TrainPhysics.compute_track_voltage`.

    That method's voltage-shape branch reads the stored `powered` flag, the
    stopped predicate, the live reservation (`received_path_plan`), and
    `eb.active` (design_new.md "Power-controller role"). A powered, stopped
    train with no reservation is the stopped-but-lit case; a train under a live
    reservation drives the speed-derived voltage.
    """
    import types
    from train_controller_types import PhysicsCommand, PHYSICS_KIND_HANDLE

    class _FakeController:
        def __init__(self):
            self.powered = powered
            self.control_mode = "ato"
            self.manual_mode_type = "simulation"
            self.manual_power_percent = 0
            self.current_speed_kmh = 0.0
            self.target_speed = target_speed
            self.eb = types.SimpleNamespace(active=False)
            self.path_projection = types.SimpleNamespace(
                received_path_plan=(object() if has_reservation else None)
            )

        def is_stopped(self):
            return self.current_speed_kmh <= 0.1

        def produce_physics_command(self):
            return PhysicsCommand(
                kind=PHYSICS_KIND_HANDLE,
                voltage_source_speed_kmh=self.target_speed,
            )

    return _FakeController()


class TestSimulatedTrackDriver:
    """Tests for SimulatedTrackDriver."""

    def test_set_and_get_voltage(self):
        """Test setting and getting voltage."""
        interface = SimulatedTrackDriver()

        result = interface.set_voltage("BLK_001", 8.0)
        assert result is True
        assert interface.get_actual_voltage("BLK_001") == 8.0

    def test_get_nonexistent_block_returns_zero(self):
        """Test getting voltage for non-existent block."""
        interface = SimulatedTrackDriver()
        assert interface.get_actual_voltage("UNKNOWN") == 0.0

    def test_get_all_voltages(self):
        """Test getting all voltages."""
        interface = SimulatedTrackDriver()
        interface.set_voltage("BLK_001", 5.0)
        interface.set_voltage("BLK_002", 8.0)
        interface.set_voltage("BLK_003", 0.0)

        voltages = interface.get_all_voltages()
        assert voltages["BLK_001"] == 5.0
        assert voltages["BLK_002"] == 8.0
        assert voltages["BLK_003"] == 0.0

    def test_clear(self):
        """Test clearing all voltages."""
        interface = SimulatedTrackDriver()
        interface.set_voltage("BLK_001", 5.0)
        interface.set_voltage("BLK_002", 8.0)

        interface.clear()
        assert interface.get_all_voltages() == {}


class TestBlockPowerController:
    """Tests for BlockPowerController."""

    @pytest.fixture
    def controller(self) -> BlockPowerController:
        """Create a basic controller wired with a fake sim_controller.

        v1: `track_voltage` is owned by `TrainPhysics` per design_new.md
        "Power-controller role". The block-power layer reads it through
        `sim_controller.get_or_create_physics(...)`. The fake sim resolves
        the controller's `state` from the test train's `state` attribute
        (registered via `register_train(...)`) and provides a real
        `TrainPhysics` instance that computes the canonical voltage.
        """
        from train_physics import TrainPhysics

        class _FakeSim:
            def __init__(self):
                self._trains_by_id = {}

            def register_train(self, train):
                self._trains_by_id[train.train_id] = train

            def get_or_create_controller(self, train_id):
                train = self._trains_by_id.get(train_id)
                powered = bool(getattr(train, "_bp_powered", False)) if train else False
                has_reservation = (
                    bool(getattr(train, "_bp_has_reservation", False)) if train else False
                )
                return _make_fake_bp_controller(
                    powered=powered, has_reservation=has_reservation, target_speed=60.0
                )

            def get_or_create_physics(self, train_id):
                return TrainPhysics(train_id=train_id)

        sim = _FakeSim()
        # Stash the fake sim so test fixtures can register their trains.
        bp = BlockPowerController()
        bp._fake_sim = sim
        return bp

    @pytest.fixture
    def train_off(self) -> Train:
        """A powered-off train."""
        train = Train(train_id="TRAIN_001")
        train._bp_powered = False
        return train

    @pytest.fixture
    def train_idle(self) -> Train:
        """A powered, stopped train with no reservation (stopped-but-lit)."""
        train = Train(train_id="TRAIN_002")
        train._bp_powered = True
        return train

    @pytest.fixture
    def train_running(self) -> Train:
        """A powered train under a live reservation (drives speed voltage).

        v1: target_speed lives on TrainController; tests pair this train with
        the `controller` fixture above which provides target_speed=60.0 via
        the fake sim_controller.
        """
        train = Train(train_id="TRAIN_003")
        train._bp_powered = True
        train._bp_has_reservation = True
        return train

    def test_set_block_voltage(self, controller: BlockPowerController):
        """Test setting block voltage."""
        result = controller.set_block_voltage("BLK_001", 8.0)
        assert result is True
        assert controller.get_block_voltage("BLK_001") == 8.0

    def test_voltage_clamping_max(self, controller: BlockPowerController):
        """Test voltage is clamped to maximum."""
        controller.set_block_voltage("BLK_001", 15.0)
        assert controller.get_block_voltage("BLK_001") == MAX_TRACK_VOLTAGE

    def test_voltage_clamping_min(self, controller: BlockPowerController):
        """A block voltage is signed, so the range is symmetric.

        The sign is the block-relative direction the train runs through that
        block, not a voltage on the rails: the rails are two 0-100% PWM
        channels into an H-bridge, and which channel a sign reaches is wiring
        the driver resolves from the rail binding below this layer.
        """
        controller.set_block_voltage("BLK_001", -15.0)
        assert controller.get_block_voltage("BLK_001") == -MAX_TRACK_VOLTAGE

    def test_release_block(self, controller: BlockPowerController):
        """Test releasing a block sets voltage to 0."""
        controller.set_block_voltage("BLK_001", 8.0)
        controller.release_block("BLK_001")
        assert controller.get_block_voltage("BLK_001") == 0.0

    def test_to_dict_from_dict_roundtrip(self, controller: BlockPowerController):
        """Test serialization roundtrip."""
        controller.set_block_voltage("BLK_001", 5.0)
        controller.set_block_voltage("BLK_002", 8.0)

        data = controller.to_dict()
        restored = BlockPowerController.from_dict(data)

        assert restored.get_block_voltage("BLK_001") == 5.0
        assert restored.get_block_voltage("BLK_002") == 8.0

    def test_from_dict_without_min_voltage_uses_the_symmetric_floor(self):
        """A dict that omits ``min_voltage`` gets the same floor a fresh
        controller has.

        ``to_dict`` always writes ``min_voltage``, so round-trips are fine, but
        a hand-written dict may omit it. The fallback must match the dataclass
        default rather than closing a range the constructor does give.
        """
        restored = BlockPowerController.from_dict({"voltages": {}})

        assert restored.min_voltage == -MAX_TRACK_VOLTAGE
        restored.set_block_voltage("BLK_001", -8.0)
        assert restored.get_block_voltage("BLK_001") == -8.0


class TestBlockCurrentVoltage:
    """`current_voltage` is not a `Block` field: voltage's canonical home is
    `TrainPhysics.compute_track_voltage(...)`, and the applied-output mirror is
    owned by `BlockPowerController` (gap-analysis 2026-09-20 M9)."""

    def test_block_has_no_current_voltage_field(self):
        """A `Block` neither stores nor serializes `current_voltage` itself."""
        block = Block(block_id="BLK_001")
        assert not hasattr(block, "current_voltage")
        assert "current_voltage" not in block.to_dict()

    def test_current_voltage_is_injected_into_the_network_payload(self):
        """The live frontend payload carries `current_voltage`, injected by
        `RailwayNetwork.to_dict` from the `BlockPowerController` applied-output
        mirror — not from a `Block` field, and not persisted."""
        from network import RailwayNetwork
        from block_power import BlockPowerController

        network = RailwayNetwork()
        network.blocks["BLK_001"] = Block(block_id="BLK_001")
        network.power_controller = BlockPowerController(network=network)
        network.power_controller.set_recorded_voltage("BLK_001", 8.5)

        payload = network.to_dict()
        assert payload["blocks"]["BLK_001"]["current_voltage"] == 8.5

        # Persistence omits it entirely (runtime state, not on the block).
        assert "current_voltage" not in network.blocks["BLK_001"].to_layout_dict()


# =============================================================================
# Phase 2 Tests - Train ATC
# =============================================================================


class TestCalculateMaxSafeSpeed:
    """Tests for calculate_max_safe_speed function."""

    def test_basic_calculation(self):
        """Test basic braking distance calculation.

        Note: The speed calculation uses model scale (1:160 by default).
        At scale, the conversion factor changes, resulting in higher speed values.
        This is correct physics for scaled model railroads.
        """
        # With 500cm clearance and 15 km/h/s deceleration
        speed = calculate_max_safe_speed(500.0, 15.0, safety_margin_cm=0.0)
        # Should be positive
        assert speed > 0
        # At 1:160 scale, speeds are higher (scaled physics)
        assert speed > 100  # Scale-adjusted calculation

    def test_zero_clearance(self):
        """Test zero clearance returns zero speed."""
        speed = calculate_max_safe_speed(0.0, 15.0)
        assert speed == 0.0

    def test_negative_clearance(self):
        """Test negative clearance returns zero speed."""
        speed = calculate_max_safe_speed(-100.0, 15.0)
        assert speed == 0.0

    def test_clearance_less_than_margin(self):
        """Test clearance less than safety margin returns zero."""
        speed = calculate_max_safe_speed(30.0, 15.0, safety_margin_cm=50.0)
        assert speed == 0.0

    def test_higher_clearance_higher_speed(self):
        """Test more clearance allows higher speed."""
        speed_200 = calculate_max_safe_speed(200.0, 15.0, safety_margin_cm=0.0)
        speed_400 = calculate_max_safe_speed(400.0, 15.0, safety_margin_cm=0.0)
        assert speed_400 > speed_200

    def test_higher_decel_higher_speed(self):
        """Test higher deceleration allows higher speed."""
        speed_15 = calculate_max_safe_speed(300.0, 15.0, safety_margin_cm=0.0)
        speed_30 = calculate_max_safe_speed(300.0, 30.0, safety_margin_cm=0.0)
        assert speed_30 > speed_15

    def test_zero_deceleration(self):
        """Test zero deceleration returns zero speed."""
        speed = calculate_max_safe_speed(500.0, 0.0)
        assert speed == 0.0


# Note: TestTrainATC class removed - TrainATC is deprecated.
# TrainController now contains the ATC logic with a different API.
# See train_controller.py for the new implementation.


class TestModelScale:
    """Tests for model_scale module."""

    def test_scale_presets_exist(self):
        """Test scale presets are defined."""
        from model_scale import SCALE_PRESETS

        assert SCALE_PRESETS["1:160"] == 160.0
        assert SCALE_PRESETS["1:87"] == 87.0

    def test_unit_conversions(self):
        """Test unit conversion functions."""
        from model_scale import kmh_to_cm_per_sec, cm_per_sec_to_kmh

        # 1 km/h ≈ 27.78 cm/s (at 1:1 scale, apply_scale=False)
        cm_s = kmh_to_cm_per_sec(1.0, apply_scale=False)
        assert 27.7 < cm_s < 27.8

        # At 1:160 scale (default N scale), speed is divided by 160
        cm_s_scaled = kmh_to_cm_per_sec(1.0, apply_scale=True)
        assert 0.17 < cm_s_scaled < 0.18  # 27.78 / 160 ≈ 0.17

        # Roundtrip (with scale)
        kmh = cm_per_sec_to_kmh(cm_s_scaled, apply_scale=True)
        assert 0.99 < kmh < 1.01

    def test_scale_setter_updates_conversion(self):
        """Test setting model scale affects conversion factor."""
        from model_scale import get_model_scale, set_model_scale, kmh_to_cm_per_sec

        original_scale = get_model_scale()
        try:
            set_model_scale(100.0)
            cm_s_scaled = kmh_to_cm_per_sec(1.0, apply_scale=True)
            assert 0.27 < cm_s_scaled < 0.28  # 27.78 / 100 ≈ 0.277
        finally:
            set_model_scale(original_scale)


# =============================================================================
# Phase 3: State Machine Tests
# =============================================================================


class TestTrainStateTransitions:
    """Tests for TrainController state transition methods."""

    def test_power_on_transition(self):
        """Test power_on_cmd() transitions OFF -> IDLE."""
        train, controller, network = create_test_train_with_controller(
            powered=False
        )
        result = controller.commands.power_on_cmd()
        assert result.success is True
        assert controller.powered
        assert controller.train_status != TrainStatus.OFF

    def test_power_on_fails_when_already_on(self):
        """Test power_on_cmd() fails when already powered."""
        train, controller, network = create_test_train_with_controller(
            powered=True
        )
        result = controller.commands.power_on_cmd()
        assert result.success is False
        assert controller.powered

    def test_power_off_when_stopped(self):
        """Test power_off_cmd() works when stopped.

        v1: power_off_cmd is an Action (teardown) latch (pending_power_off); the OFF
        transition happens in Action teardown. Acceptance is immediate.
        """
        train, controller, network = create_test_train_with_controller(
            powered=True
        )
        result = controller.commands.power_off_cmd()
        assert result.success is True
        assert controller.route_hooks.phase_latches.power_off is True

    def test_power_off_fails_when_moving(self):
        """Test power_off_cmd() fails when moving."""
        train, controller, network = create_test_train_with_controller(
            powered=True, current_speed=50.0
        )
        result = controller.commands.power_off_cmd()
        assert result.success is False
        assert controller.powered and not controller.is_stopped()

    def test_path_backed_manual_run_still_completes_to_standby(self):
        """A path-backed run brakes to a stand after ATO->manual.

        `train_status` is derived, so there is no reactive state transition to
        drive here; the post-physics callback just applies the zero-speed
        update and the train reads stopped. This drives that callback with a
        no-boundary zero-speed update — the real per-tick order (tick() then
        physics).
        """
        train, controller, network = create_test_train_with_controller(
            powered=True
        )
        controller.commands.control_mode = "manual"
        controller._apply_head_frame("end_a")
        # `path_backed_run_active` is a derived projection (per design_new.md
        # ~line 161, no controller-owned setter); the assertion below confirms
        # the projection reads False when no path/route/destination context
        # exists for this stub controller.
        controller.commands.engaged = False

        span = set_train_span(controller, (0, 0), facing_direction=Direction.NORTH)
        controller.tick(0.1)
        controller.on_physics_updated(
            event_time=0.0,
            span_update=PhysicsSpanUpdate(
                new_span=span,
                current_speed_kmh=0.0,
                added_tiles=[],
                removed_tiles=[],
            ),
        )

        assert controller.is_stopped()
        assert controller.path_projection.path_backed_run_active is False

    def test_emergency_stop_cmd_from_running(self):
        """Test activate_emergency_cmd() from RUNNING_MOVING."""
        train, controller, network = create_test_train_with_controller(
            powered=True
        )
        # Per design_new.md "Train ownership", EB state lives on the controller;
        # set the speed via the controller (live runtime field) and assert
        # against `eb_active` rather than a Train-side `emergency_brake`
        # mirror.
        controller.physics_sync.current_speed_kmh = 80.0
        result = controller.eb.activate()
        assert result.success is True
        assert controller.eb.active is True

    def test_emergency_stop_cmd_always_allowed(self):
        """Test activate_emergency_cmd() works from any state."""
        train, controller, network = create_test_train_with_controller(
            powered=True
        )
        result = controller.eb.activate()
        assert result.success is True
        assert controller.eb.active is True


    def test_set_mode_manual_fails_when_moving(self):
        """set_mode('manual') is rejected while moving."""
        train, controller, network = create_test_train_with_controller(
            powered=True, current_speed=30.0
        )
        result = controller.commands.set_mode("manual")
        assert result.success is False

    def test_set_mode_clears_retained_manual_power_percent(self):
        """A mode change clears `manual.manual_power_percent` (design_new.md
        "ATO status").

        The percentage is set-and-forget: nothing re-asserts it per tick, so a
        value parked before the mode change would become the train's command
        after it with no fresh driver input. The EB path is already covered by
        `produce_physics_command()`, which zeroes the field on every tick while
        `eb.active` — but a plain manual -> ato -> manual cycle never enters
        that branch, which is the hole this closes.
        """
        train, controller, network = create_test_train_with_controller(
            powered=True
        )
        controller.commands.power_on_cmd()
        controller.commands.set_mode("manual")
        controller._apply_head_frame("end_a")
        controller.commands.set_manual_mode_type("power_percent")
        assert controller.commands.set_manual_power_percent(60).success is True
        assert controller.manual.manual_power_percent == 60

        assert controller.commands.set_mode("ato").success is True
        assert controller.manual.manual_power_percent == 0, (
            "set_mode('ato') must clear the retained power percent; a value "
            "surviving the mode change commands the train with no fresh "
            "driver input."
        )

        # And returning to manual leaves nothing to re-apply: the submode is
        # still power_percent, so a surviving value would command motion here.
        controller.commands.set_manual_power_percent(45)
        assert controller.commands.set_mode("manual").success is True
        assert controller.manual.manual_mode_type == "power_percent"
        assert controller.manual.manual_power_percent == 0

    def test_set_mode_rejected_during_open_calibration(self):
        """set_mode is rejected while a calibration leg or run is open: a
        calibration pass drives the train itself and outranks the ATO/manual
        sources, so its driving mode must not change underneath it — the
        operator ends the pass first (design_new.md "set_mode", "calibration
        outranks ATO"). The open-pass signal is
        `SpeedCalibration.get_propulsion_override(train_id) is not None`, read
        through the controller's injected SpeedCalibration.
        """
        from types import SimpleNamespace

        train, controller, network = create_test_train_with_controller(
            powered=True  # stopped -> passes the is_stopped() gate
        )
        # An open pass returns a non-None override for this train. (A stopped
        # auto pass returns value=0.0 — still non-None — and a lighting run is
        # always non-None; both must block the mode change.)
        override = SimpleNamespace(kind="power_percent", value=0.0)
        controller.wire_dependencies(
            speed_calibration=SimpleNamespace(
                get_propulsion_override=(
                    lambda tid: override if tid == controller.train_id else None
                )
            )
        )
        # Train is stopped with a valid mode, so the ONLY remaining reason to
        # reject is the calibration guard.
        result = controller.commands.set_mode("manual")
        assert result.success is False, (
            "set_mode must be rejected while a calibration pass is open"
        )
        assert "calibration" in result.message.lower()

    def test_set_mode_allowed_when_no_calibration_open(self):
        """The guard is scoped to an open pass: with no override
        (`get_propulsion_override` returns None), a stopped train's set_mode
        still succeeds — the ordinary path is unchanged.
        """
        from types import SimpleNamespace

        train, controller, network = create_test_train_with_controller(
            powered=True
        )
        controller.wire_dependencies(
            speed_calibration=SimpleNamespace(
                get_propulsion_override=lambda tid: None
            )
        )
        assert controller.commands.set_mode("ato").success is True




class TestCompleteStateFlows:
    """Integration tests for complete state transition flows via TrainController."""

    def test_normal_startup_to_powered_flow(self):
        """OFF -> powered; selecting manual leaves it MANUAL. `train_status` is
        derived, so there is no separate READY/RUNNING_MOVING state to reach."""
        train, controller, network = create_test_train_with_controller(
            powered=False
        )

        # Seed the controller span so a facing direction is available.
        set_train_span(controller, (0, 0), facing_direction=Direction.NORTH)
        assert controller.train_status == TrainStatus.OFF

        # Power on
        result = controller.commands.power_on_cmd()
        assert result.success is True
        assert controller.powered

        # Frame the head end (manual mode).
        controller.commands.control_mode = "manual"
        assert controller._apply_head_frame("end_a") is None
        assert controller.train_status == TrainStatus.MANUAL

    def test_run_completion_writes_no_controller_state(self):
        """Stop completion writes no controller state: `on_path_complete()` is a
        no-op and the derived status is unaffected (design_new.md step `09b0`).

        v1: route assignment lives on `RouteExecutor.execution_states[train_id]`,
        not on `Train`. This test only exercises the controller.
        """
        train, controller, network = create_test_train_with_controller(
            powered=True
        )
        controller._apply_head_frame("end_a")

        controller.on_path_complete()
        assert controller.powered and controller.is_stopped()

        # Re-frame in manual and complete again: still a no-op.
        set_train_span(controller, (0, 0), facing_direction=Direction.NORTH)
        controller.commands.control_mode = "manual"
        controller._apply_head_frame("end_a")
        controller.on_path_complete()
        assert controller.powered and controller.is_stopped()


    def test_emergency_stop_flow(self):
        """Test emergency stop flow via TrainController."""
        train, controller, network = create_test_train_with_controller(
            powered=True, current_speed=80.0
        )

        # Emergency stop
        result = controller.eb.activate()
        assert result.success is True
        assert controller.eb.active is True
        # Emergency stop sets handle to EB
        assert controller.ops.handle_position.value == "EB"

        # Cannot release emergency until stopped and handle in N/B
        controller.physics_sync.current_speed_kmh = 0.0  # Simulate stopping
        # Handle is still EB, need to set to N first
        controller.ops.handle_position = HandlePosition.N
        result = controller.eb.release()
        assert result.success is True
        assert controller.eb.active is False

    def test_eb_springs_the_drivers_retained_power_percent(self):
        """EB is a supervisory brake-only override that springs both driver
        inputs to their safe rest (design_new.md "ATO status"): activation
        re-zeros `manual_power_percent`, so no retained percentage survives to
        drive the train when EB lifts."""
        train, controller, network = create_test_train_with_controller(
            powered=True, current_speed=0.0
        )
        assert controller.commands.set_mode("manual").success is True
        controller.manual.manual_mode_type = "power_percent"
        controller.manual.manual_power_percent = 55.0

        controller.eb.activate()

        assert controller.manual.manual_power_percent == 0.0

    def test_eb_release_is_not_gated_on_the_power_notch(self):
        """There is no driver-control interlock: EB springs the notch to 0, so a
        release reads only the physical-safety conditions (design_new.md
        "ATO status")."""
        train, controller, network = create_test_train_with_controller(
            powered=True, current_speed=0.0
        )
        assert controller.commands.set_mode("manual").success is True
        controller.manual.manual_mode_type = "power_percent"
        controller.manual.manual_power_percent = 55.0
        controller.eb.activate()

        result = controller.eb.release()

        assert result.success is True
        assert controller.eb.active is False
        # The notch was sprung by EB, so nothing drives the train after release.
        assert controller.manual.manual_power_percent == 0.0

    def test_shutdown_flow(self):
        """Test complete shutdown flow.

        v1: power_off_cmd is an Action (teardown) latch; OFF transition deferred.
        Train.target_speed was deleted.
        """
        train, controller, network = create_test_train_with_controller(
            powered=True, current_speed=50.0
        )

        # Cannot power off while moving
        result = controller.commands.power_off_cmd()
        assert result.success is False
        assert controller.powered and not controller.is_stopped()

        # Stop first - simulate stopping
        controller.physics_sync.current_speed_kmh = 0.0

        # Now can power off — acceptance is immediate, OFF deferred to Action teardown.
        result = controller.commands.power_off_cmd()
        assert result.success is True
        assert controller.route_hooks.phase_latches.power_off is True


class TestHandleUnderEmergencyBrake:
    """
    Handle / power-percent input while the emergency brake is latched.

    Per design_new.md "Power/cab/mode command contracts": ``set_handle()`` is
    accepted while
    ``eb.active == True`` (the operator may move the lever to any position),
    but EB overrides motion and the handle NEVER releases EB. EB is released
    only by ``eb.release()`` (gated on stopped + cancel consumed + no live
    authority).
    """

    def test_release_allows_eb_handle_when_stopped(self):
        """eb.release() succeeds when stopped even though the handle is EB."""
        train, controller, _ = create_test_train_with_controller(
            powered=True, current_speed=40.0
        )

        result = controller.eb.activate()
        assert result.success is True

        controller.physics_sync.current_speed_kmh = 0.0
        result = controller.eb.release()

        assert result.success is True
        assert controller.eb.active is False

    def test_eb_release_writes_no_operational_state(self):
        """eb.release() clears only eb.active. `train_status` is derived, so the
        train immediately reads its underlying (non-EB) status with no separate
        state write (design_new.md "EB release")."""
        train, controller, _ = create_test_train_with_controller(
            powered=True, current_speed=40.0
        )
        controller.eb.activate()  # no live authority -> clean latch
        controller.physics_sync.current_speed_kmh = 0.0
        assert controller.train_status == TrainStatus.EB

        result = controller.eb.release()
        assert result.success is True
        assert controller.eb.active is False
        # No separate state write: the derived status is no longer EB.
        assert controller.train_status != TrainStatus.EB

    def test_set_handle_accepted_under_eb_does_not_release(self):
        """set_handle(N) is accepted while EB is latched but does NOT release EB."""
        train, controller, _ = create_test_train_with_controller(
            powered=True, current_speed=30.0
        )
        # set_handle is a manual-mode command (design_new.md "Power/cab/mode
        # command contracts"); put the
        # controller in manual mode with a cab so the lever input is accepted.
        controller.commands.control_mode = "manual"
        controller._apply_head_frame("end_a")

        result = controller.eb.activate()
        assert result.success is True

        controller.physics_sync.current_speed_kmh = 0.0
        result = controller.commands.set_handle(HandlePosition.N)

        # The lever moves, but EB stays latched: the handle is not a
        # release channel.
        assert result.success is True
        assert controller.ops.handle_position == HandlePosition.N
        assert controller.eb.active is True

    def test_set_handle_accepted_under_eb_while_moving(self):
        """set_handle is accepted under EB even while moving; EB still overrides motion."""
        train, controller, _ = create_test_train_with_controller(
            powered=True, current_speed=30.0
        )
        # set_handle is a manual-mode command (design_new.md "Power/cab/mode
        # command contracts").
        controller.commands.control_mode = "manual"
        controller._apply_head_frame("end_a")

        result = controller.eb.activate()
        assert result.success is True

        result = controller.commands.set_handle(HandlePosition.N)

        assert result.success is True
        assert controller.eb.active is True

    def test_set_handle_power_position_accepted_under_eb(self):
        """A power position is accepted under EB (stored), but EB still overrides motion."""
        train, controller, _ = create_test_train_with_controller(
            powered=True, current_speed=0.0
        )
        # set_handle is a manual-mode command (design_new.md "Power/cab/mode
        # command contracts").
        controller.commands.control_mode = "manual"
        controller._apply_head_frame("end_a")

        result = controller.eb.activate()
        assert result.success is True

        result = controller.commands.set_handle(HandlePosition.P1)

        assert result.success is True
        assert controller.ops.handle_position == HandlePosition.P1
        assert controller.eb.active is True
