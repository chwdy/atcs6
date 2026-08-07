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
from enums import Direction, TrainState, BlockState, HandlePosition
from network import RailwayNetwork
from train_controller import TrainController, create_controller_for_train
from runtime_payloads import PhysicsSpanUpdate
from tests.train_test_helpers import set_train_span


# =============================================================================
# Controller Test Helpers
# =============================================================================


def create_test_train_with_controller(
    train_id: str = "T001",
    state: TrainState = TrainState.OFF,
    current_speed: float = 0.0,
    max_speed: float = 100.0,
) -> tuple[Train, TrainController, RailwayNetwork]:
    """
    Create a train with a controller for testing.

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

    # Per design_new.md ~line 1292, live runtime state lives on the
    # controller; tests configure it directly there instead of mirroring
    # through Train.
    controller.physics_sync.current_speed_kmh = current_speed
    if state == TrainState.OFF:
        controller.ops.state = TrainState.OFF
    else:
        controller.ops.state = state

    # Sync controller speed (canonical per design_new.md)
    controller.physics_sync.current_speed_kmh = float(current_speed)

    return train, controller, network


# =============================================================================
# SpeedProfile Tests
# =============================================================================


class TestSpeedProfile:
    """Tests for SpeedProfile class."""

    @pytest.fixture
    def basic_profile(self) -> SpeedProfile:
        """Create a basic speed profile for testing.

        Uses the default two-stage calibration (power_speed_curve
        [(0,0),(100,100)] + voltage_power_curve synthesized linear from
        lighting_voltage (0%) to max_voltage (100%) per design_new.md
        "SpeedProfile"). With lighting_voltage=2.0 / max_voltage=12.0 this is
        [(2,0),(12,100)], which maps 30 -> 5 V, 60 -> 8 V, 120 -> 12 V.
        """
        return SpeedProfile(
            profile_id="test_profile",
            name="Test Profile",
            lighting_voltage=2.0,
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

    def test_speed_to_voltage_at_calibration_points(self, basic_profile: SpeedProfile):
        """Two-stage conversion returns exact values at calibration points."""
        assert basic_profile.speed_to_voltage_two_stage(0.0) == 0.0
        assert basic_profile.speed_to_voltage_two_stage(30.0) == 5.0
        assert basic_profile.speed_to_voltage_two_stage(60.0) == 8.0
        assert basic_profile.speed_to_voltage_two_stage(120.0) == 12.0

    def test_speed_to_voltage_interpolation(self, basic_profile: SpeedProfile):
        """Two-stage conversion interpolates between points."""
        # 45 km/h -> 45% -> 2 + 0.45*(12-2) = 6.5V
        voltage_45 = basic_profile.speed_to_voltage_two_stage(45.0)
        assert 6.4 < voltage_45 < 6.6

        # 15 km/h -> 15% -> 2 + 0.15*(12-2) = 3.5V
        voltage_15 = basic_profile.speed_to_voltage_two_stage(15.0)
        assert 3.4 < voltage_15 < 3.6

    def test_speed_to_voltage_clamps_max(self, basic_profile: SpeedProfile):
        """Two-stage conversion clamps at maximum."""
        assert basic_profile.speed_to_voltage_two_stage(150.0) == 12.0
        assert basic_profile.speed_to_voltage_two_stage(200.0) == 12.0

    def test_speed_to_voltage_zero_and_negative(self, basic_profile: SpeedProfile):
        """Two-stage conversion outputs 0 V for zero and negative speed."""
        assert basic_profile.speed_to_voltage_two_stage(0.0) == 0.0
        assert basic_profile.speed_to_voltage_two_stage(-10.0) == 0.0

    def test_to_dict_from_dict_roundtrip(self, basic_profile: SpeedProfile):
        """Test serialization roundtrip."""
        data = basic_profile.to_dict()
        restored = SpeedProfile.from_dict(data)

        assert restored.profile_id == basic_profile.profile_id
        assert restored.name == basic_profile.name
        assert restored.lighting_voltage == basic_profile.lighting_voltage
        assert restored.acceleration_rate == basic_profile.acceleration_rate
        assert len(restored.power_speed_curve) == len(
            basic_profile.power_speed_curve
        )
        assert len(restored.voltage_power_curve) == len(
            basic_profile.voltage_power_curve
        )

    def test_default_speed_profile_exists(self):
        """Test DEFAULT_SPEED_PROFILE is properly defined."""
        assert DEFAULT_SPEED_PROFILE is not None
        assert DEFAULT_SPEED_PROFILE.profile_id == "default"
        assert len(DEFAULT_SPEED_PROFILE.power_speed_curve) > 0
        assert len(DEFAULT_SPEED_PROFILE.voltage_power_curve) > 0
        # Should convert speeds reasonably
        assert DEFAULT_SPEED_PROFILE.speed_to_voltage_two_stage(60.0) > 0


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
        assert task.should_depart(now=25.0, dwell_started_at=0.0) is False

        # Enough time elapsed
        assert task.should_depart(now=30.0, dwell_started_at=0.0) is True
        assert task.should_depart(now=35.0, dwell_started_at=0.0) is True

    def test_should_depart_absolute_mode(self):
        """Test departure with absolute timing (target_time)."""
        task = RouteTask(
            task_kind="dwell",
            task_location=PlatformTarget(platform_id="STN_001"),
            target_time=500.0,  # Leave at simulation time 500
        )

        # Before target time
        assert task.should_depart(now=450.0, dwell_started_at=400.0) is False

        # At target time
        assert task.should_depart(now=500.0, dwell_started_at=400.0) is True

        # After target time
        assert task.should_depart(now=550.0, dwell_started_at=400.0) is True

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
        assert simple_route.is_active is False

    def test_activate_deactivate(self, simple_route: Route):
        """Test route activation."""
        simple_route.activate(100.0)
        assert simple_route.is_active is True
        assert simple_route.started_at == 100.0

        simple_route.deactivate()
        assert simple_route.is_active is False

    def test_to_dict_from_dict_roundtrip(self, simple_route: Route):
        """Test route serialization."""
        simple_route.activate(100.0)

        data = simple_route.to_dict()
        restored = Route.from_dict(data)

        assert restored.route_id == simple_route.route_id
        assert restored.name == simple_route.name
        assert len(restored.tasks) == len(simple_route.tasks)
        assert restored.mode == simple_route.mode
        assert restored.is_active == simple_route.is_active
        assert restored.started_at == simple_route.started_at


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
        controller.commands.cab = "A"
        controller.ops.state = TrainState.OFF
        return train, controller

    def test_new_properties_initialization(
        self, basic_train: tuple[Train, TrainController]
    ):
        """Test new Phase 1 properties (Train-side persistent fields only).

        Per design_new.md ~line 1292, `state` and `travel_direction` live
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

        v1: power_off_cmd sets pending_power_off latch consumed in Phase 3.
        Acceptance is immediate; OFF transition happens after Phase 3.
        """
        train, controller, network = create_test_train_with_controller(
            state=TrainState.OFF
        )

        # Power on
        result = controller.commands.power_on_cmd()
        assert result.success is True
        assert controller.ops.state == TrainState.READY
        assert controller.ops.state == TrainState.READY

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
            state=TrainState.RUNNING_MOVING, current_speed=50.0
        )

        result = controller.commands.power_off_cmd()
        assert result.success is False
        assert controller.ops.state == TrainState.RUNNING_MOVING


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
        # Per design_new.md ~line 1292, Train no longer mirrors live
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
# TrainState Enum Tests
# =============================================================================


class TestTrainState:
    """Tests for TrainState enum."""




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
        from train_controller_types import (
            PhysicsCommand,
            PHYSICS_KIND_HANDLE,
        )

        class _FakeController:
            def __init__(self, state):
                self.state = state
                self.control_mode = "ato"
                self.manual_mode_type = "simulation"
                self.manual_power_percent = 0
                self.current_speed_kmh = 0.0
                self.target_speed = 60.0

            def produce_physics_command(self):
                return PhysicsCommand(
                    kind=PHYSICS_KIND_HANDLE,
                    voltage_source_speed_kmh=self.target_speed,
                )

        class _FakeSim:
            def __init__(self):
                self._trains_by_id = {}

            def register_train(self, train):
                self._trains_by_id[train.train_id] = train

            def get_or_create_controller(self, train_id):
                train = self._trains_by_id.get(train_id)
                state = getattr(train, "state", TrainState.OFF) if train else TrainState.OFF
                return _FakeController(state)

            def get_or_create_physics(self, train_id):
                return TrainPhysics(train_id=train_id)

        sim = _FakeSim()
        # Stash the fake sim so test fixtures can register their trains.
        bp = BlockPowerController(_sim_controller=sim)
        bp._fake_sim = sim
        return bp

    @pytest.fixture
    def train_off(self) -> Train:
        """Create a train in OFF state."""
        train = Train(train_id="TRAIN_001")
        train.state = TrainState.OFF
        return train

    @pytest.fixture
    def train_idle(self) -> Train:
        """Create a train in IDLE state."""
        train = Train(train_id="TRAIN_002")
        train.state = TrainState.READY
        return train

    @pytest.fixture
    def train_running(self) -> Train:
        """Create a train in RUNNING_MOVING state.

        v1: target_speed lives on TrainController; tests pair this train with
        the `controller` fixture above which provides target_speed=60.0 via
        the fake sim_controller.
        """
        train = Train(train_id="TRAIN_003")
        train.state = TrainState.RUNNING_MOVING
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

    def test_calculate_voltage_for_off_train(
        self, controller: BlockPowerController, train_off: Train
    ):
        """Test voltage calculation for OFF state."""
        controller._fake_sim.register_train(train_off)
        voltage = controller._calculate_voltage_for_train(train_off)
        assert voltage == 0.0

    def test_calculate_voltage_for_idle_train(
        self, controller: BlockPowerController, train_idle: Train
    ):
        """Test voltage calculation for IDLE state."""
        controller._fake_sim.register_train(train_idle)
        # No SpeedProfile supplied; lighting voltage falls through to 0V at
        # the physics layer (the fake controller has no profile reference).
        # Pass the default lighting voltage through a minimal SpeedProfile.
        profile = SpeedProfile(
            profile_id="test",
            name="Test",
            lighting_voltage=2.0,
        )
        voltage = controller._calculate_voltage_for_train(train_idle, profile)
        assert voltage == 2.0

    def test_calculate_voltage_for_running_train(
        self, controller: BlockPowerController, train_running: Train
    ):
        """Test voltage calculation for RUNNING_MOVING state."""
        controller._fake_sim.register_train(train_running)
        # Without speed profile, uses linear approximation
        voltage = controller._calculate_voltage_for_train(train_running)
        # 60 km/h / 10 = 6V (linear approximation)
        assert voltage == 6.0

    def test_calculate_voltage_with_speed_profile(
        self, controller: BlockPowerController, train_running: Train
    ):
        """Test voltage calculation with speed profile."""
        controller._fake_sim.register_train(train_running)
        profile = SpeedProfile(
            profile_id="test",
            name="Test",
        )
        voltage = controller._calculate_voltage_for_train(train_running, profile)
        # The default voltage->power curve is linear from this profile's own
        # lighting_voltage (0%) to max_voltage (100%), so 60 km/h -> 60% power
        # -> 1.5 + 0.6 * (12.0 - 1.5).
        assert voltage == pytest.approx(7.8)


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
    """Tests for Block.current_voltage property."""

    def test_block_has_current_voltage(self):
        """Test Block has current_voltage property."""
        block = Block(block_id="BLK_001")
        assert block.current_voltage == 0.0

    def test_block_current_voltage_in_frontend_payload_but_not_persisted(self):
        """`current_voltage` is exposed in the live frontend payload (to_dict)
        but is runtime state (design_new.md Out of Scope: "Persistence /
        save-load of runtime state | In-memory only"), so from_dict must not
        restore it from disk — voltage defaults back to 0.0.
        """
        block = Block(block_id="BLK_001")
        block.current_voltage = 8.5

        # Frontend payload still carries the live value.
        data = block.to_dict()
        assert data["current_voltage"] == 8.5

        # Reload (the persistence path uses Block.to_layout_dict which omits
        # current_voltage entirely; this stricter case feeds the legacy
        # to_dict output through from_dict and confirms runtime fields are
        # still dropped).
        restored = Block.from_dict(data)
        assert restored.current_voltage == 0.0


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
            state=TrainState.OFF
        )
        result = controller.commands.power_on_cmd()
        assert result.success is True
        assert controller.ops.state == TrainState.READY
        assert controller.ops.state == TrainState.READY

    def test_power_on_fails_when_already_on(self):
        """Test power_on_cmd() fails when already powered."""
        train, controller, network = create_test_train_with_controller(
            state=TrainState.READY
        )
        result = controller.commands.power_on_cmd()
        assert result.success is False
        assert controller.ops.state == TrainState.READY

    def test_power_off_when_stopped(self):
        """Test power_off_cmd() works when stopped.

        v1: power_off_cmd is a Phase-3 latch (pending_power_off); the OFF
        transition happens in Phase 3. Acceptance is immediate.
        """
        train, controller, network = create_test_train_with_controller(
            state=TrainState.READY
        )
        result = controller.commands.power_off_cmd()
        assert result.success is True
        assert controller.route_hooks.phase_latches.power_off is True

    def test_power_off_fails_when_moving(self):
        """Test power_off_cmd() fails when moving."""
        train, controller, network = create_test_train_with_controller(
            state=TrainState.RUNNING_MOVING, current_speed=50.0
        )
        result = controller.commands.power_off_cmd()
        assert result.success is False
        assert controller.ops.state == TrainState.RUNNING_MOVING

    def test_set_cab_transition(self):
        """set_cab(\"A\") leaves the controller in READY with cab selected."""
        train, controller, network = create_test_train_with_controller(
            state=TrainState.READY
        )
        set_train_span(controller, (0, 0), facing_direction=Direction.NORTH)
        controller.commands.control_mode = "manual"  # cab selection is manual-only
        result = controller.commands.set_cab("A")
        assert result.success is True
        assert controller.ops.state == TrainState.READY
        assert controller.commands.cab == "A"

    def test_depart_transition(self):
        """Test depart() transitions READY -> RUNNING_MOVING (manual mode)."""
        from unittest.mock import MagicMock
        train, controller, network = create_test_train_with_controller(
            state=TrainState.READY
        )
        set_train_span(controller, (0, 0), facing_direction=Direction.NORTH)
        # Use manual mode to test depart transition (not ATO auto-cab logic).
        # Manual depart now requires a cab to be selected explicitly.
        controller.commands.control_mode = "manual"
        controller.commands.cab = "A"
        # Per design_new.md line 1812: depart() requires an active path plan.
        # Stub the path dispatcher to satisfy the received_path_plan precondition.
        mock_plan = MagicMock()
        mock_dispatcher = MagicMock()
        mock_dispatcher.get_active_path.return_value = mock_plan
        controller.path_dispatcher = mock_dispatcher
        success, reason = controller.depart()
        assert success is True
        assert reason is None
        assert controller.ops.state == TrainState.RUNNING_MOVING


    def test_path_backed_manual_run_still_completes_to_standby(self):
        """A path-backed run still completes to standby (READY) after ATO->manual.

        The RUNNING_MOVING -> READY transition for a stopped train with no live
        authority is the reactive speed-based evaluation in
        ``on_physics_updated()`` (design_new.md "Reactive speed-based state
        evaluation"); per steps 89ba/32dd/90d3, ``tick()`` must not perform it.
        This drives that canonical post-physics callback with a no-boundary
        zero-speed update — the real per-tick order (tick() then physics).
        """
        train, controller, network = create_test_train_with_controller(
            state=TrainState.RUNNING_MOVING
        )
        train.state = TrainState.RUNNING_MOVING
        controller.ops.state = TrainState.RUNNING_MOVING
        controller.commands.control_mode = "manual"
        controller.commands.cab = "A"
        # `path_backed_run_active` is a derived projection (per design_new.md
        # ~line 161, no controller-owned setter); the assertion below confirms
        # the projection reads False when no path/route/destination context
        # exists for this stub controller.
        controller.ato.running = False

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

        assert controller.ops.state == TrainState.READY
        assert controller.path_projection.path_backed_run_active is False

    def test_depart_fails_without_direction(self):
        """Test depart() fails without facing direction."""
        from unittest.mock import MagicMock
        train, controller, network = create_test_train_with_controller(
            state=TrainState.READY
        )
        # Per design_new.md line 1812: depart() requires an active path plan
        # before checking other preconditions (direction, state).
        mock_plan = MagicMock()
        mock_dispatcher = MagicMock()
        mock_dispatcher.get_active_path.return_value = mock_plan
        controller.path_dispatcher = mock_dispatcher
        controller.commands.control_mode = "manual"
        controller.commands.cab = "A"
        # No span seeded -> controller has no facing direction.
        controller.physics_sync.current_span = None
        success, reason = controller.depart()
        assert success is False
        assert reason is not None

    def test_depart_fails_from_wrong_state(self):
        """Test depart() fails from non-READY/RUNNING_MOVING state (e.g. AT_STATION)."""
        from unittest.mock import MagicMock
        train, controller, network = create_test_train_with_controller(
            state=TrainState.RUNNING_AT_STATION
        )
        # Per design_new.md line 1812: depart() requires an active path plan.
        mock_plan = MagicMock()
        mock_dispatcher = MagicMock()
        mock_dispatcher.get_active_path.return_value = mock_plan
        controller.path_dispatcher = mock_dispatcher
        set_train_span(controller, (0, 0), facing_direction=Direction.NORTH)
        success, reason = controller.depart()
        assert success is False
        assert reason is not None



    def test_enter_station_dwell_transition(self):
        """enter_station_dwell() transitions READY -> RUNNING_AT_STATION.

        Per design_new.md:1516/1901 dwell entry is valid only from the
        dispatcher-completed READY state: stop completion lands the train in
        READY (on_path_complete), and the next Phase-1 dwell-entry step calls
        enter_station_dwell() from there.
        """
        train, controller, network = create_test_train_with_controller(
            state=TrainState.READY
        )
        ok, reason = controller.enter_station_dwell()
        assert ok, reason
        assert controller.ops.state == TrainState.RUNNING_AT_STATION

    def test_enter_station_dwell_rejected_from_running_moving(self):
        """enter_station_dwell() is rejected (state unchanged) when not in
        READY — the documented acceptance precondition (design_new.md:1901)."""
        train, controller, network = create_test_train_with_controller(
            state=TrainState.RUNNING_MOVING
        )
        ok, reason = controller.enter_station_dwell()
        assert not ok
        assert controller.ops.state == TrainState.RUNNING_MOVING

    def test_finish_dwell_transition(self):
        """finish_dwell() transitions RUNNING_AT_STATION -> READY."""
        train, controller, network = create_test_train_with_controller(
            state=TrainState.RUNNING_AT_STATION
        )
        controller.finish_dwell()
        assert controller.ops.state == TrainState.READY

    def test_emergency_stop_cmd_from_running(self):
        """Test activate_emergency_cmd() from RUNNING_MOVING."""
        train, controller, network = create_test_train_with_controller(
            state=TrainState.RUNNING_MOVING
        )
        # Per design_new.md ~line 1292, EB state lives on the controller;
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
            state=TrainState.READY
        )
        result = controller.eb.activate()
        assert result.success is True
        assert controller.eb.active is True


    def test_set_mode_manual_fails_when_moving(self):
        """set_mode('manual') is rejected while moving."""
        train, controller, network = create_test_train_with_controller(
            state=TrainState.READY, current_speed=30.0
        )
        result = controller.commands.set_mode("manual")
        assert result.success is False




class TestStateBasedVoltageOutput:
    """Tests for state-based voltage output in BlockPowerController.

    v1: `track_voltage` is owned by `TrainPhysics` per design_new.md
    "Power-controller role". `BlockPowerController` resolves the scalar
    through `sim_controller.get_or_create_physics(...)`. These tests wire
    a fake sim that returns a controller with the appropriate `state` and
    a real `TrainPhysics` for the canonical computation.
    """

    @staticmethod
    def _make_sim(state, target_speed=0.0):
        from train_physics import TrainPhysics
        from train_controller_types import (
            PhysicsCommand,
            PHYSICS_KIND_HANDLE,
        )

        class _FakeController:
            def __init__(self):
                self.state = state
                self.control_mode = "ato"
                self.manual_mode_type = "simulation"
                self.manual_power_percent = 0
                self.current_speed_kmh = 0.0
                self.target_speed = target_speed

            def produce_physics_command(self):
                return PhysicsCommand(
                    kind=PHYSICS_KIND_HANDLE,
                    voltage_source_speed_kmh=self.target_speed,
                )

        class _FakeSim:
            def get_or_create_controller(self, _train_id):
                return _FakeController()

            def get_or_create_physics(self, train_id):
                return TrainPhysics(train_id=train_id)

        return _FakeSim()

    def test_voltage_for_off_state(self):
        """Test voltage is 0 for OFF state."""
        from block_power import BlockPowerController
        from train import Train

        train = Train(train_id="T001")
        controller = BlockPowerController(_sim_controller=self._make_sim(TrainState.OFF))
        voltage = controller._calculate_voltage_for_train(train)
        assert voltage == 0.0

    def test_voltage_for_idle_state(self):
        """Test voltage is lighting voltage for IDLE state."""
        from block_power import BlockPowerController
        from speed_profile import SpeedProfile
        from train import Train

        train = Train(train_id="T001")
        controller = BlockPowerController(_sim_controller=self._make_sim(TrainState.READY))
        profile = SpeedProfile(profile_id="t", name="T", lighting_voltage=2.5)
        voltage = controller._calculate_voltage_for_train(train, profile)
        assert voltage == 2.5

    def test_voltage_for_standby_state(self):
        """Test voltage is lighting voltage for STANDBY state."""
        from block_power import BlockPowerController
        from speed_profile import SpeedProfile
        from train import Train

        train = Train(train_id="T001")
        controller = BlockPowerController(_sim_controller=self._make_sim(TrainState.READY))
        profile = SpeedProfile(profile_id="t", name="T", lighting_voltage=2.5)
        voltage = controller._calculate_voltage_for_train(train, profile)
        assert voltage == 2.5

    def test_voltage_for_running_at_station_state(self):
        """Test voltage is lighting voltage for RUNNING_AT_STATION state."""
        from block_power import BlockPowerController
        from speed_profile import SpeedProfile
        from train import Train

        train = Train(train_id="T001")
        controller = BlockPowerController(
            _sim_controller=self._make_sim(TrainState.RUNNING_AT_STATION)
        )
        # Lighting voltage is a per-train property on the SpeedProfile; the
        # controller emits it for RUNNING_AT_STATION.
        profile = SpeedProfile(
            profile_id="t", name="T",
            lighting_voltage=2.0,
        )
        voltage = controller._calculate_voltage_for_train(train, profile)
        assert voltage == 2.0

    def test_voltage_for_running_moving_state_uses_speed(self):
        """Voltage uses controller-derived target_speed for RUNNING_MOVING."""
        from block_power import BlockPowerController
        from speed_profile import SpeedProfile
        from train import Train

        train = Train(train_id="T001")
        controller = BlockPowerController(
            _sim_controller=self._make_sim(TrainState.RUNNING_MOVING, target_speed=60.0)
        )
        profile = SpeedProfile(
            profile_id="test",
            name="Test",
        )
        voltage = controller._calculate_voltage_for_train(train, profile)
        # Default curve anchors: lighting_voltage 1.5 (0%) -> max_voltage 12.0
        # (100%), so 60 km/h -> 60% power -> 7.8 V.
        assert voltage == pytest.approx(7.8)



class TestCompleteStateFlows:
    """Integration tests for complete state transition flows via TrainController."""

    def test_normal_startup_to_running_flow(self):
        """Test complete flow: OFF -> IDLE -> STANDBY -> RUNNING_MOVING."""
        train, controller, network = create_test_train_with_controller(
            state=TrainState.OFF
        )

        # Seed the controller span so a facing direction is available.
        set_train_span(controller, (0, 0), facing_direction=Direction.NORTH)

        # Power on
        result = controller.commands.power_on_cmd()
        assert result.success is True
        assert controller.ops.state == TrainState.READY

        # Select cab (manual mode: cab selection is manual-only); stays READY.
        controller.commands.control_mode = "manual"
        assert controller.commands.set_cab("A").success is True
        assert controller.ops.state == TrainState.READY

        # Depart (manual mode to bypass ATO auto-cab selection).
        # Per design_new.md line 1812: depart() now returns (bool, Optional[str]).
        from unittest.mock import MagicMock
        controller.commands.control_mode = "manual"
        mock_plan = MagicMock()
        mock_dispatcher = MagicMock()
        mock_dispatcher.get_active_path.return_value = mock_plan
        controller.path_dispatcher = mock_dispatcher
        success, _ = controller.depart()
        assert success is True
        assert controller.ops.state == TrainState.RUNNING_MOVING

    def test_running_to_station_to_complete_flow(self):
        """Test complete flow through station arrival and route completion.

        v1: route assignment lives on `RouteExecutor.execution_states[train_id]`,
        not on `Train`. This test only exercises controller state transitions;
        route preservation is covered by `RouteExecutor` integration tests.
        """
        train, controller, network = create_test_train_with_controller(
            state=TrainState.RUNNING_MOVING
        )

        # Stop completion lands the train in READY first (on_path_complete),
        # which is the only state dwell entry is valid from
        # (design_new.md:1516/1901).
        controller.on_path_complete()
        assert controller.ops.state == TrainState.READY

        # Phase-1 dwell entry transitions READY -> RUNNING_AT_STATION.
        ok, reason = controller.enter_station_dwell()
        assert ok, reason
        assert controller.ops.state == TrainState.RUNNING_AT_STATION

        # Phase-1 dwell complete transitions back to READY.
        ok, reason = controller.finish_dwell()
        assert ok, reason
        assert controller.ops.state == TrainState.READY

        # Simulate moving and arriving at final station
        # Need to set controller state too for proper flow
        from unittest.mock import MagicMock
        set_train_span(controller, (0, 0), facing_direction=Direction.NORTH)
        # Use manual mode to test depart (not ATO auto-cab logic). Manual
        # depart now requires a cab to be selected explicitly.
        controller.commands.control_mode = "manual"
        controller.commands.cab = "A"
        # Per design_new.md line 1812: depart() requires an active path plan.
        mock_plan = MagicMock()
        mock_dispatcher = MagicMock()
        mock_dispatcher.get_active_path.return_value = mock_plan
        controller.path_dispatcher = mock_dispatcher
        controller.depart()
        assert controller.ops.state == TrainState.RUNNING_MOVING

        # Arriving at the station completes the path into READY first
        # (on_path_complete), the only state dwell entry is valid from.
        controller.on_path_complete()
        assert controller.ops.state == TrainState.READY

        ok, reason = controller.enter_station_dwell()
        assert ok, reason
        assert controller.ops.state == TrainState.RUNNING_AT_STATION

        # Route complete (controller-side state transition only; route
        # teardown is owned by RouteExecutor in v1).
        ok, reason = controller.finish_dwell()
        assert ok, reason
        assert controller.ops.state == TrainState.READY


    def test_emergency_stop_flow(self):
        """Test emergency stop flow via TrainController."""
        train, controller, network = create_test_train_with_controller(
            state=TrainState.RUNNING_MOVING, current_speed=80.0
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

    def test_shutdown_flow(self):
        """Test complete shutdown flow.

        v1: power_off_cmd is a Phase-3 latch; OFF transition deferred.
        Train.target_speed was deleted.
        """
        train, controller, network = create_test_train_with_controller(
            state=TrainState.RUNNING_MOVING, current_speed=50.0
        )

        # Cannot power off while moving
        result = controller.commands.power_off_cmd()
        assert result.success is False
        assert controller.ops.state == TrainState.RUNNING_MOVING

        # Stop first - simulate stopping
        controller.physics_sync.current_speed_kmh = 0.0
        controller.ops.state = TrainState.READY
        train.state = TrainState.READY

        # Now can power off — acceptance is immediate, OFF deferred to Phase 3.
        result = controller.commands.power_off_cmd()
        assert result.success is True
        assert controller.route_hooks.phase_latches.power_off is True


class TestHandleUnderEmergencyBrake:
    """
    Handle / power-percent input while the emergency brake is latched.

    Per design_new.md line 1731: ``set_handle()`` is accepted while
    ``eb.active == True`` (the operator may move the lever to any position),
    but EB overrides motion and the handle NEVER releases EB. EB is released
    only by ``eb.release()`` (gated on stopped + cancel consumed + no live
    authority).
    """

    def test_release_allows_eb_handle_when_stopped(self):
        """eb.release() succeeds when stopped even though the handle is EB."""
        train, controller, _ = create_test_train_with_controller(
            state=TrainState.RUNNING_MOVING, current_speed=40.0
        )

        result = controller.eb.activate()
        assert result.success is True

        controller.physics_sync.current_speed_kmh = 0.0
        result = controller.eb.release()

        assert result.success is True
        assert controller.eb.active is False

    def test_set_handle_accepted_under_eb_does_not_release(self):
        """set_handle(N) is accepted while EB is latched but does NOT release EB."""
        train, controller, _ = create_test_train_with_controller(
            state=TrainState.RUNNING_MOVING, current_speed=30.0
        )
        # set_handle is a manual-mode command (design_new.md line 1757); put the
        # controller in manual mode with a cab so the lever input is accepted.
        controller.commands.control_mode = "manual"
        controller.commands.cab = "A"

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
            state=TrainState.RUNNING_MOVING, current_speed=30.0
        )
        # set_handle is a manual-mode command (design_new.md line 1757).
        controller.commands.control_mode = "manual"
        controller.commands.cab = "A"

        result = controller.eb.activate()
        assert result.success is True

        result = controller.commands.set_handle(HandlePosition.N)

        assert result.success is True
        assert controller.eb.active is True

    def test_set_handle_power_position_accepted_under_eb(self):
        """A power position is accepted under EB (stored), but EB still overrides motion."""
        train, controller, _ = create_test_train_with_controller(
            state=TrainState.RUNNING_MOVING, current_speed=0.0
        )
        # set_handle is a manual-mode command (design_new.md line 1757).
        controller.commands.control_mode = "manual"
        controller.commands.cab = "A"

        result = controller.eb.activate()
        assert result.success is True

        result = controller.commands.set_handle(HandlePosition.P1)

        assert result.success is True
        assert controller.ops.handle_position == HandlePosition.P1
        assert controller.eb.active is True
