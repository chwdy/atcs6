"""
Tests for Phase 1 - Train System Data Structures

This module tests:
- SpeedProfile: speed/voltage conversion, acceleration/deceleration
- Route & Waypoint: route traversal, timing modes
- Train modifications: new properties, turn_around, state management
"""

import pytest
from speed_profile import SpeedProfile, DEFAULT_SPEED_PROFILE
from train_route import Route, Waypoint, TimingMode, RouteMode
from train import Train, TrainCar
from enums import Direction, TrainState, BlockState, HandlePosition
from network import RailwayNetwork
from train_controller import TrainController, create_controller_for_train


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
        current_speed=current_speed,
    )
    train.state = state
    network.add_train(train)
    controller = create_controller_for_train(train_id, network)

    # Sync controller state with train state
    if state == TrainState.OFF:
        controller.power_on = False
        controller.state = TrainState.OFF
    else:
        controller.power_on = True
        controller.state = state

    return train, controller, network


# =============================================================================
# SpeedProfile Tests
# =============================================================================


class TestSpeedProfile:
    """Tests for SpeedProfile class."""

    @pytest.fixture
    def basic_profile(self) -> SpeedProfile:
        """Create a basic speed profile for testing."""
        return SpeedProfile(
            profile_id="test_profile",
            name="Test Profile",
            speed_voltage_curve=[
                (0.0, 0.0),
                (30.0, 5.0),
                (60.0, 8.0),
                (120.0, 12.0),
            ],
            min_start_voltage=2.0,
            lighting_voltage=2.5,
            acceleration_rate=10.0,
            deceleration_rate=15.0,
            emergency_brake_rate=30.0,
        )

    def test_profile_initialization(self, basic_profile: SpeedProfile):
        """Test profile is created correctly."""
        assert basic_profile.profile_id == "test_profile"
        assert basic_profile.name == "Test Profile"
        assert basic_profile.lighting_voltage == 2.5
        assert basic_profile.acceleration_rate == 10.0
        assert basic_profile.deceleration_rate == 15.0
        assert basic_profile.emergency_brake_rate == 30.0

    def test_speed_to_voltage_at_calibration_points(self, basic_profile: SpeedProfile):
        """Test speed_to_voltage returns exact values at calibration points."""
        assert basic_profile.speed_to_voltage(0.0) == 0.0
        assert basic_profile.speed_to_voltage(30.0) == 5.0
        assert basic_profile.speed_to_voltage(60.0) == 8.0
        assert basic_profile.speed_to_voltage(120.0) == 12.0

    def test_speed_to_voltage_interpolation(self, basic_profile: SpeedProfile):
        """Test speed_to_voltage interpolates between points."""
        # Midpoint between (30, 5) and (60, 8) -> 45 km/h should give 6.5V
        voltage_45 = basic_profile.speed_to_voltage(45.0)
        assert 6.4 < voltage_45 < 6.6

        # Quarter point: 15 km/h between (0, 0) and (30, 5) -> ~2.5V
        voltage_15 = basic_profile.speed_to_voltage(15.0)
        assert 2.4 < voltage_15 < 2.6

    def test_speed_to_voltage_clamps_max(self, basic_profile: SpeedProfile):
        """Test speed_to_voltage clamps at maximum."""
        assert basic_profile.speed_to_voltage(150.0) == 12.0
        assert basic_profile.speed_to_voltage(200.0) == 12.0

    def test_speed_to_voltage_zero_and_negative(self, basic_profile: SpeedProfile):
        """Test speed_to_voltage handles zero and negative speed."""
        assert basic_profile.speed_to_voltage(0.0) == 0.0
        assert basic_profile.speed_to_voltage(-10.0) == 0.0

    def test_voltage_to_speed_at_calibration_points(self, basic_profile: SpeedProfile):
        """Test voltage_to_speed returns correct values."""
        assert basic_profile.voltage_to_speed(0.0) == 0.0
        assert basic_profile.voltage_to_speed(5.0) == 30.0
        assert basic_profile.voltage_to_speed(8.0) == 60.0
        assert basic_profile.voltage_to_speed(12.0) == 120.0

    def test_voltage_to_speed_interpolation(self, basic_profile: SpeedProfile):
        """Test voltage_to_speed interpolates."""
        # 6.5V is between 5V (30 km/h) and 8V (60 km/h)
        speed = basic_profile.voltage_to_speed(6.5)
        assert 44 < speed < 46

    def test_calculate_new_speed_acceleration(self, basic_profile: SpeedProfile):
        """Test acceleration calculation."""
        # accelerate at 10 km/h per second for 1 second
        new_speed = basic_profile.calculate_new_speed(0.0, 100.0, 1.0)
        assert new_speed == 10.0

        # accelerate for 2 seconds
        new_speed = basic_profile.calculate_new_speed(0.0, 100.0, 2.0)
        assert new_speed == 20.0

    def test_calculate_new_speed_deceleration(self, basic_profile: SpeedProfile):
        """Test deceleration calculation."""
        # decelerate at 15 km/h per second for 1 second
        new_speed = basic_profile.calculate_new_speed(60.0, 0.0, 1.0)
        assert new_speed == 45.0

        # decelerate for 4 seconds (should reach target)
        new_speed = basic_profile.calculate_new_speed(60.0, 0.0, 4.0)
        assert new_speed == 0.0

    def test_calculate_new_speed_emergency(self, basic_profile: SpeedProfile):
        """Test emergency braking calculation."""
        # emergency brake at 30 km/h per second for 1 second
        new_speed = basic_profile.calculate_new_speed(60.0, 0.0, 1.0, is_emergency=True)
        assert new_speed == 30.0

        # emergency brake for 2 seconds
        new_speed = basic_profile.calculate_new_speed(60.0, 0.0, 2.0, is_emergency=True)
        assert new_speed == 0.0

    def test_calculate_new_speed_at_target(self, basic_profile: SpeedProfile):
        """Test no change when at target speed."""
        new_speed = basic_profile.calculate_new_speed(50.0, 50.0, 1.0)
        assert new_speed == 50.0

    def test_braking_distance_calculation(self, basic_profile: SpeedProfile):
        """Test braking distance calculation."""
        # From 60 km/h to 0 at 15 km/h/s
        distance = basic_profile.get_braking_distance_cm(60.0, 0.0)
        # Rough estimate: should be positive and reasonable
        assert distance > 0
        assert distance < 10000  # Less than 100m

    def test_braking_distance_emergency(self, basic_profile: SpeedProfile):
        """Test emergency braking distance is shorter."""
        normal_dist = basic_profile.get_braking_distance_cm(
            60.0, 0.0, is_emergency=False
        )
        emergency_dist = basic_profile.get_braking_distance_cm(
            60.0, 0.0, is_emergency=True
        )
        assert emergency_dist < normal_dist

    def test_to_dict_from_dict_roundtrip(self, basic_profile: SpeedProfile):
        """Test serialization roundtrip."""
        data = basic_profile.to_dict()
        restored = SpeedProfile.from_dict(data)

        assert restored.profile_id == basic_profile.profile_id
        assert restored.name == basic_profile.name
        assert restored.lighting_voltage == basic_profile.lighting_voltage
        assert restored.acceleration_rate == basic_profile.acceleration_rate
        assert len(restored.speed_voltage_curve) == len(
            basic_profile.speed_voltage_curve
        )

    def test_default_speed_profile_exists(self):
        """Test DEFAULT_SPEED_PROFILE is properly defined."""
        assert DEFAULT_SPEED_PROFILE is not None
        assert DEFAULT_SPEED_PROFILE.profile_id == "default"
        assert len(DEFAULT_SPEED_PROFILE.speed_voltage_curve) > 0
        # Should convert speeds reasonably
        assert DEFAULT_SPEED_PROFILE.speed_to_voltage(60.0) > 0


# =============================================================================
# Waypoint Tests
# =============================================================================


class TestWaypoint:
    """Tests for Waypoint class."""

    def test_waypoint_initialization(self):
        """Test waypoint default values."""
        wp = Waypoint(station_id="STN_001")
        assert wp.station_id == "STN_001"
        assert wp.dwell_time_sec == 30.0
        assert wp.timing_mode == TimingMode.NONE
        assert wp.target_time is None

    def test_should_depart_none_mode(self):
        """Test departure with no timing constraint."""
        wp = Waypoint(station_id="STN_001", dwell_time_sec=30.0)

        # Not enough time yet
        assert wp.should_depart(25.0, 0.0, 100.0) is False

        # Enough time
        assert wp.should_depart(30.0, 0.0, 100.0) is True
        assert wp.should_depart(35.0, 0.0, 100.0) is True

    def test_should_depart_relative_mode(self):
        """Test departure with relative timing."""
        wp = Waypoint(
            station_id="STN_001",
            timing_mode=TimingMode.RELATIVE,
            target_time=60.0,  # 60 seconds after route start
        )

        # Route started at t=100, current time = 150 (50s elapsed)
        assert wp.should_depart(30.0, 100.0, 150.0) is False

        # 60s elapsed
        assert wp.should_depart(30.0, 100.0, 160.0) is True

        # 70s elapsed
        assert wp.should_depart(30.0, 100.0, 170.0) is True

    def test_should_depart_absolute_mode(self):
        """Test departure with absolute timing."""
        wp = Waypoint(
            station_id="STN_001",
            timing_mode=TimingMode.ABSOLUTE,
            target_time=500.0,  # Leave at simulation time 500
        )

        # Before target time
        assert wp.should_depart(60.0, 100.0, 450.0) is False

        # At target time
        assert wp.should_depart(60.0, 100.0, 500.0) is True

        # After target time
        assert wp.should_depart(60.0, 100.0, 550.0) is True

    def test_to_dict_from_dict_roundtrip(self):
        """Test waypoint serialization."""
        wp = Waypoint(
            station_id="STN_002",
            dwell_time_sec=45.0,
            timing_mode=TimingMode.RELATIVE,
            target_time=120.0,
        )

        data = wp.to_dict()
        restored = Waypoint.from_dict(data)

        assert restored.station_id == wp.station_id
        assert restored.dwell_time_sec == wp.dwell_time_sec
        assert restored.timing_mode == wp.timing_mode
        assert restored.target_time == wp.target_time


# =============================================================================
# Route Tests
# =============================================================================


class TestRoute:
    """Tests for Route class."""

    @pytest.fixture
    def simple_route(self) -> Route:
        """Create a simple 3-waypoint route."""
        return Route(
            route_id="ROUTE_001",
            name="Test Route",
            waypoints=[
                Waypoint(station_id="A"),
                Waypoint(station_id="B"),
                Waypoint(station_id="C"),
            ],
            mode=RouteMode.ONE_WAY,
        )

    def test_route_initialization(self, simple_route: Route):
        """Test route default values."""
        assert simple_route.route_id == "ROUTE_001"
        assert simple_route.name == "Test Route"
        assert len(simple_route.waypoints) == 3
        assert simple_route.mode == RouteMode.ONE_WAY
        assert simple_route.current_waypoint_index == 0
        assert simple_route.direction == 1
        assert simple_route.is_active is False

    def test_get_current_waypoint(self, simple_route: Route):
        """Test getting current waypoint."""
        wp = simple_route.get_current_waypoint()
        assert wp is not None
        assert wp.station_id == "A"

    def test_advance_one_way(self, simple_route: Route):
        """Test ONE_WAY route advancement."""
        assert simple_route.get_next_station_id() == "A"

        wp = simple_route.advance_to_next_waypoint()
        assert wp is not None
        assert wp.station_id == "B"

        wp = simple_route.advance_to_next_waypoint()
        assert wp is not None
        assert wp.station_id == "C"

        # Should complete after C
        wp = simple_route.advance_to_next_waypoint()
        assert wp is None
        assert simple_route.is_complete() is True

    def test_advance_loop(self):
        """Test LOOP route advancement."""
        route = Route(
            route_id="LOOP_001",
            waypoints=[
                Waypoint(station_id="A"),
                Waypoint(station_id="B"),
                Waypoint(station_id="C"),
            ],
            mode=RouteMode.LOOP,
        )

        # Go through A -> B -> C -> A (wrap)
        assert route.get_next_station_id() == "A"
        route.advance_to_next_waypoint()
        assert route.get_next_station_id() == "B"
        route.advance_to_next_waypoint()
        assert route.get_next_station_id() == "C"
        route.advance_to_next_waypoint()
        assert route.get_next_station_id() == "A"  # Wrapped!

        # LOOP never completes
        assert route.is_complete() is False

    def test_advance_round_trip(self):
        """Test ROUND_TRIP route advancement."""
        route = Route(
            route_id="RT_001",
            waypoints=[
                Waypoint(station_id="A"),
                Waypoint(station_id="B"),
                Waypoint(station_id="C"),
            ],
            mode=RouteMode.ROUND_TRIP,
        )

        # A -> B -> C -> B -> A -> B -> ...
        assert route.get_next_station_id() == "A"

        route.advance_to_next_waypoint()
        assert route.get_next_station_id() == "B"

        route.advance_to_next_waypoint()
        assert route.get_next_station_id() == "C"

        # Now reverse direction
        route.advance_to_next_waypoint()
        assert route.get_next_station_id() == "B"
        assert route.direction == -1

        route.advance_to_next_waypoint()
        assert route.get_next_station_id() == "A"

        # Reverse again
        route.advance_to_next_waypoint()
        assert route.get_next_station_id() == "B"
        assert route.direction == 1

        # ROUND_TRIP never completes
        assert route.is_complete() is False

    def test_activate_deactivate(self, simple_route: Route):
        """Test route activation."""
        simple_route.activate(100.0)
        assert simple_route.is_active is True
        assert simple_route.started_at == 100.0
        assert simple_route.current_waypoint_index == 0

        simple_route.deactivate()
        assert simple_route.is_active is False

    def test_reset(self, simple_route: Route):
        """Test route reset."""
        simple_route.activate(100.0)
        simple_route.advance_to_next_waypoint()
        simple_route.set_error("Test error", 150.0)

        simple_route.reset()
        assert simple_route.current_waypoint_index == 0
        assert simple_route.is_active is False
        assert simple_route.started_at is None
        assert simple_route.last_error is None

    def test_error_handling(self, simple_route: Route):
        """Test error tracking."""
        simple_route.set_error("Path not found", 200.0)
        assert simple_route.last_error == "Path not found"
        assert simple_route.error_time == 200.0

        simple_route.clear_error()
        assert simple_route.last_error is None
        assert simple_route.error_time is None

    def test_to_dict_from_dict_roundtrip(self, simple_route: Route):
        """Test route serialization."""
        simple_route.activate(100.0)
        simple_route.advance_to_next_waypoint()

        data = simple_route.to_dict()
        restored = Route.from_dict(data)

        assert restored.route_id == simple_route.route_id
        assert restored.name == simple_route.name
        assert len(restored.waypoints) == len(simple_route.waypoints)
        assert restored.mode == simple_route.mode
        assert restored.current_waypoint_index == simple_route.current_waypoint_index
        assert restored.is_active == simple_route.is_active
        assert restored.started_at == simple_route.started_at


# =============================================================================
# Train Phase 1 Tests
# =============================================================================


class TestTrainPhase1:
    """Tests for Train class Phase 1 modifications."""

    @pytest.fixture
    def basic_train(self) -> Train:
        """Create a basic train for testing."""
        train = Train(
            train_id="TRAIN_001",
            max_speed=100.0,
        )
        train.model = "BR 218"
        train.min_radius_cm = 40.0
        train.facing_direction = Direction.EAST
        train.state = TrainState.OFF
        return train

    def test_new_properties_initialization(self, basic_train: Train):
        """Test new Phase 1 properties."""
        assert basic_train.model == "BR 218"
        assert basic_train.min_radius_cm == 40.0
        assert basic_train.facing_direction == Direction.EAST
        assert basic_train.travel_direction == "forward"
        assert basic_train.state == TrainState.OFF
        assert basic_train.speed_profile_id is None
        assert basic_train.assigned_route_id is None
        assert basic_train.target_speed == 0.0
        assert basic_train.manual_target_speed == 0.0

    def test_turn_around_when_stopped(self, basic_train: Train):
        """Test turn_around when train is stopped."""
        basic_train.current_speed = 0.0
        basic_train.facing_direction = Direction.EAST

        result = basic_train.turn_around()
        assert result is True
        assert basic_train.facing_direction == Direction.WEST

        # Turn around again
        result = basic_train.turn_around()
        assert result is True
        assert basic_train.facing_direction == Direction.EAST

    def test_turn_around_when_moving_fails(self, basic_train: Train):
        """Test turn_around fails when train is moving."""
        basic_train.current_speed = 50.0
        basic_train.facing_direction = Direction.EAST

        result = basic_train.turn_around()
        assert result is False
        assert basic_train.facing_direction == Direction.EAST  # Unchanged

    def test_turn_around_no_facing_direction(self, basic_train: Train):
        """Test turn_around fails when no facing direction."""
        basic_train.current_speed = 0.0
        basic_train.facing_direction = None

        result = basic_train.turn_around()
        assert result is False

    def test_get_effective_direction_forward(self, basic_train: Train):
        """Test effective direction in forward mode."""
        basic_train.facing_direction = Direction.NORTH
        basic_train.travel_direction = "forward"

        assert basic_train.get_effective_direction() == Direction.NORTH

    def test_get_effective_direction_reverse(self, basic_train: Train):
        """Test effective direction in reverse mode."""
        basic_train.facing_direction = Direction.NORTH
        basic_train.travel_direction = "reverse"

        assert basic_train.get_effective_direction() == Direction.SOUTH

    def test_get_effective_direction_no_facing(self, basic_train: Train):
        """Test effective direction when no facing direction."""
        basic_train.facing_direction = None
        assert basic_train.get_effective_direction() is None

    def test_is_stopped(self, basic_train: Train):
        """Test is_stopped check."""
        basic_train.current_speed = 0.0
        assert basic_train.is_stopped() is True

        basic_train.current_speed = 0.05
        assert basic_train.is_stopped() is True  # Below threshold

        basic_train.current_speed = 1.0
        assert basic_train.is_stopped() is False

    def test_power_on_off(self):
        """Test power_on_cmd and power_off_cmd via TrainController."""
        train, controller, network = create_test_train_with_controller(
            state=TrainState.OFF
        )

        # Power on
        result = controller.power_on_cmd()
        assert result.success is True
        assert train.state == TrainState.IDLE
        assert controller.state == TrainState.IDLE

        # Can't power on again
        result = controller.power_on_cmd()
        assert result.success is False

        # Power off
        result = controller.power_off_cmd()
        assert result.success is True
        assert train.state == TrainState.OFF
        assert controller.state == TrainState.OFF

    def test_power_off_while_moving_fails(self):
        """Test power_off_cmd fails when train is moving."""
        train, controller, network = create_test_train_with_controller(
            state=TrainState.RUNNING_MOVING, current_speed=50.0
        )

        result = controller.power_off_cmd()
        assert result.success is False
        assert train.state == TrainState.RUNNING_MOVING

    def test_set_standby(self):
        """Test set_direction_and_standby via TrainController."""
        train, controller, network = create_test_train_with_controller(
            state=TrainState.IDLE
        )

        result = controller.set_direction_and_standby(Direction.NORTH)
        assert result is True
        assert train.state == TrainState.STANDBY
        assert train.facing_direction == Direction.NORTH

    def test_set_standby_not_idle_fails(self):
        """Test set_direction_and_standby fails when not IDLE."""
        train, controller, network = create_test_train_with_controller(
            state=TrainState.OFF
        )

        result = controller.set_direction_and_standby(Direction.NORTH)
        assert result is False

    def test_enable_disable_manual(self):
        """Test enable_manual and disable_manual via TrainController."""
        train, controller, network = create_test_train_with_controller(
            state=TrainState.IDLE
        )
        train.facing_direction = Direction.EAST

        # Enable manual
        result = controller.enable_manual()
        assert result is True
        assert train.state == TrainState.MANUAL

        # Disable manual - should go to STANDBY (has facing direction)
        result = controller.disable_manual()
        assert result is True
        assert train.state == TrainState.STANDBY

    def test_enable_manual_off_fails(self):
        """Test enable_manual fails when OFF."""
        train, controller, network = create_test_train_with_controller(
            state=TrainState.OFF
        )
        result = controller.enable_manual()
        assert result is False

    def test_set_manual_speed(self, basic_train: Train):
        """Test setting manual speed."""
        basic_train.state = TrainState.MANUAL
        basic_train.max_speed = 100.0

        result = basic_train.set_manual_speed(50.0)
        assert result is True
        assert basic_train.manual_target_speed == 50.0

        # Clamps to max
        result = basic_train.set_manual_speed(150.0)
        assert result is True
        assert basic_train.manual_target_speed == 100.0

        # Clamps to 0
        result = basic_train.set_manual_speed(-10.0)
        assert result is True
        assert basic_train.manual_target_speed == 0.0

    def test_set_manual_speed_not_manual_fails(self, basic_train: Train):
        """Test set_manual_speed fails when not in MANUAL mode."""
        basic_train.state = TrainState.IDLE

        result = basic_train.set_manual_speed(50.0)
        assert result is False

    def test_to_dict_includes_new_properties(self, basic_train: Train):
        """Test to_dict includes Phase 1 properties."""
        basic_train.model = "Test Model"
        basic_train.min_radius_cm = 35.0
        basic_train.facing_direction = Direction.SOUTH
        basic_train.travel_direction = "reverse"
        basic_train.state = TrainState.MANUAL
        basic_train.speed_profile_id = "profile_001"
        basic_train.assigned_route_id = "route_001"
        basic_train.target_speed = 60.0
        basic_train.manual_target_speed = 45.0

        data = basic_train.to_dict()

        assert data["model"] == "Test Model"
        assert data["min_radius_cm"] == 35.0
        assert data["facing_direction"] == "SOUTH"
        assert data["travel_direction"] == "reverse"
        assert data["state"] == "manual"
        assert data["speed_profile_id"] == "profile_001"
        assert data["assigned_route_id"] == "route_001"
        assert data["target_speed"] == 60.0
        assert data["manual_target_speed"] == 45.0

    def test_from_dict_restores_new_properties(self, basic_train: Train):
        """Test from_dict restores Phase 1 properties."""
        data = {
            "id": "TRAIN_002",
            "max_speed": 120.0,
            "speed": 30.0,
            "model": "Shinkansen E5",
            "min_radius_cm": 50.0,
            "facing_direction": "WEST",
            "travel_direction": "forward",
            "state": "standby",
            "speed_profile_id": "profile_002",
            "assigned_route_id": "route_002",
            "target_speed": 80.0,
            "manual_target_speed": 0.0,
        }

        train = Train.from_dict(data)

        assert train.train_id == "TRAIN_002"
        assert train.model == "Shinkansen E5"
        assert train.min_radius_cm == 50.0
        assert train.facing_direction == Direction.WEST
        assert train.travel_direction == "forward"
        assert train.state == TrainState.STANDBY
        assert train.speed_profile_id == "profile_002"
        assert train.assigned_route_id == "route_002"
        assert train.target_speed == 80.0
        assert train.manual_target_speed == 0.0

    def test_from_dict_handles_missing_new_properties(self):
        """Test from_dict handles missing Phase 1 properties (backwards compat)."""
        data = {
            "id": "TRAIN_OLD",
            "max_speed": 80.0,
            "speed": 0.0,
        }

        train = Train.from_dict(data)

        # Should use defaults
        assert train.model == ""
        assert train.min_radius_cm == 30.0
        assert train.facing_direction is None
        assert train.travel_direction == "forward"
        assert train.state == TrainState.OFF
        assert train.speed_profile_id is None


# =============================================================================
# TrainState Enum Tests
# =============================================================================


class TestTrainState:
    """Tests for TrainState enum."""

    def test_all_states_exist(self):
        """Test all 7 states are defined."""
        assert TrainState.OFF.value == "off"
        assert TrainState.IDLE.value == "idle"
        assert TrainState.STANDBY.value == "standby"
        assert TrainState.RUNNING_MOVING.value == "running_moving"
        assert TrainState.RUNNING_WAITING.value == "running_waiting"
        assert TrainState.RUNNING_AT_STATION.value == "running_at_station"
        assert TrainState.MANUAL.value == "manual"

    def test_state_from_value(self):
        """Test creating state from string value."""
        assert TrainState("off") == TrainState.OFF
        assert TrainState("manual") == TrainState.MANUAL
        assert TrainState("running_moving") == TrainState.RUNNING_MOVING


# =============================================================================
# Phase 2 Tests - Block Power Controller
# =============================================================================

from block_power import (
    BlockPowerController,
    SimulatedOutputInterface,
    OutputInterface,
)
from block import Block
from train_controller import (
    calculate_max_safe_speed,
    ATC_SAFETY_MARGIN_CM,
    YELLOW_SIGNAL_SPEED_KMH,
)
from block_power import MAX_TRACK_VOLTAGE, DEFAULT_LIGHTING_VOLTAGE


class TestSimulatedOutputInterface:
    """Tests for SimulatedOutputInterface."""

    def test_set_and_get_voltage(self):
        """Test setting and getting voltage."""
        interface = SimulatedOutputInterface()

        result = interface.set_voltage("BLK_001", 8.0)
        assert result is True
        assert interface.get_actual_voltage("BLK_001") == 8.0

    def test_get_nonexistent_block_returns_zero(self):
        """Test getting voltage for non-existent block."""
        interface = SimulatedOutputInterface()
        assert interface.get_actual_voltage("UNKNOWN") == 0.0

    def test_get_all_voltages(self):
        """Test getting all voltages."""
        interface = SimulatedOutputInterface()
        interface.set_voltage("BLK_001", 5.0)
        interface.set_voltage("BLK_002", 8.0)
        interface.set_voltage("BLK_003", 0.0)

        voltages = interface.get_all_voltages()
        assert voltages["BLK_001"] == 5.0
        assert voltages["BLK_002"] == 8.0
        assert voltages["BLK_003"] == 0.0

    def test_clear(self):
        """Test clearing all voltages."""
        interface = SimulatedOutputInterface()
        interface.set_voltage("BLK_001", 5.0)
        interface.set_voltage("BLK_002", 8.0)

        interface.clear()
        assert interface.get_all_voltages() == {}


class TestBlockPowerController:
    """Tests for BlockPowerController."""

    @pytest.fixture
    def controller(self) -> BlockPowerController:
        """Create a basic controller for testing."""
        return BlockPowerController()

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
        train.state = TrainState.IDLE
        return train

    @pytest.fixture
    def train_running(self) -> Train:
        """Create a train in RUNNING_MOVING state."""
        train = Train(train_id="TRAIN_003")
        train.state = TrainState.RUNNING_MOVING
        train.target_speed = 60.0
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
        """Test voltage is clamped to minimum."""
        # Phase 6: min_voltage is now -MAX_TRACK_VOLTAGE to support opposite polarity
        controller.set_block_voltage("BLK_001", -15.0)
        assert controller.get_block_voltage("BLK_001") == -MAX_TRACK_VOLTAGE

    def test_calculate_voltage_for_off_train(
        self, controller: BlockPowerController, train_off: Train
    ):
        """Test voltage calculation for OFF state."""
        voltage = controller._calculate_voltage_for_train(train_off)
        assert voltage == 0.0

    def test_calculate_voltage_for_idle_train(
        self, controller: BlockPowerController, train_idle: Train
    ):
        """Test voltage calculation for IDLE state."""
        voltage = controller._calculate_voltage_for_train(train_idle)
        assert voltage == DEFAULT_LIGHTING_VOLTAGE

    def test_calculate_voltage_for_running_train(
        self, controller: BlockPowerController, train_running: Train
    ):
        """Test voltage calculation for RUNNING_MOVING state."""
        # Without speed profile, uses linear approximation
        voltage = controller._calculate_voltage_for_train(train_running)
        # 60 km/h / 10 = 6V (linear approximation)
        assert voltage == 6.0

    def test_calculate_voltage_with_speed_profile(
        self, controller: BlockPowerController, train_running: Train
    ):
        """Test voltage calculation with speed profile."""
        profile = SpeedProfile(
            profile_id="test",
            name="Test",
            speed_voltage_curve=[(0, 0), (60, 8), (120, 12)],
        )
        voltage = controller._calculate_voltage_for_train(train_running, profile)
        assert voltage == 8.0

    def test_calculate_voltage_for_manual_mode(self, controller: BlockPowerController):
        """Test voltage calculation for MANUAL mode."""
        train = Train(train_id="TRAIN_004")
        train.state = TrainState.MANUAL
        train.manual_target_speed = 40.0

        voltage = controller._calculate_voltage_for_train(train)
        # 40 km/h / 10 = 4V (linear approximation)
        assert voltage == 4.0

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


class TestBlockCurrentVoltage:
    """Tests for Block.current_voltage property."""

    def test_block_has_current_voltage(self):
        """Test Block has current_voltage property."""
        block = Block(block_id="BLK_001")
        assert block.current_voltage == 0.0

    def test_block_current_voltage_serialization(self):
        """Test current_voltage is serialized."""
        block = Block(block_id="BLK_001")
        block.current_voltage = 8.5

        data = block.to_dict()
        assert data["current_voltage"] == 8.5

        restored = Block.from_dict(data)
        assert restored.current_voltage == 8.5


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


class TestTrainStateManager:
    """Tests for TrainStateManager class."""

    @pytest.fixture
    def test_train(self) -> Train:
        """Create a train for state machine testing."""
        train = Train(
            train_id="T001",
            max_speed=100.0,
            current_speed=0.0,
        )
        train.state = TrainState.OFF
        train.facing_direction = None
        return train

    def test_state_manager_import(self):
        """Test that state manager can be imported."""
        from train_state_manager import TrainStateManager, StateTransitionResult

        assert TrainStateManager is not None
        assert StateTransitionResult is not None

    def test_can_transition_same_state(self, test_train: Train):
        """Test transitioning to same state is always valid."""
        from train_state_manager import TrainStateManager

        test_train.state = TrainState.IDLE
        result = TrainStateManager.can_transition(test_train, TrainState.IDLE)
        assert result.success is True

    def test_can_transition_off_to_idle(self, test_train: Train):
        """Test OFF -> IDLE transition."""
        from train_state_manager import TrainStateManager

        test_train.state = TrainState.OFF
        result = TrainStateManager.can_transition(test_train, TrainState.IDLE)
        assert result.success is True

    def test_can_transition_idle_to_off_when_stopped(self, test_train: Train):
        """Test IDLE -> OFF transition when stopped."""
        from train_state_manager import TrainStateManager

        test_train.state = TrainState.IDLE
        test_train.current_speed = 0.0
        result = TrainStateManager.can_transition(test_train, TrainState.OFF)
        assert result.success is True

    def test_cannot_transition_idle_to_off_when_moving(self, test_train: Train):
        """Test IDLE -> OFF fails when moving."""
        from train_state_manager import TrainStateManager

        test_train.state = TrainState.IDLE
        test_train.current_speed = 30.0
        result = TrainStateManager.can_transition(test_train, TrainState.OFF)
        assert result.success is False
        assert "moving" in result.error.lower()

    def test_can_transition_idle_to_standby(self, test_train: Train):
        """Test IDLE -> STANDBY transition."""
        from train_state_manager import TrainStateManager

        test_train.state = TrainState.IDLE
        result = TrainStateManager.can_transition(test_train, TrainState.STANDBY)
        assert result.success is True

    def test_can_transition_standby_to_running_moving(self, test_train: Train):
        """Test STANDBY -> RUNNING_MOVING transition."""
        from train_state_manager import TrainStateManager

        test_train.state = TrainState.STANDBY
        test_train.facing_direction = Direction.NORTH
        result = TrainStateManager.can_transition(test_train, TrainState.RUNNING_MOVING)
        assert result.success is True

    def test_cannot_transition_standby_to_running_without_direction(
        self, test_train: Train
    ):
        """Test STANDBY -> RUNNING_MOVING fails without direction."""
        from train_state_manager import TrainStateManager

        test_train.state = TrainState.STANDBY
        test_train.facing_direction = None
        result = TrainStateManager.can_transition(test_train, TrainState.RUNNING_MOVING)
        assert result.success is False
        assert "direction" in result.error.lower()

    def test_can_transition_to_manual_when_stopped(self, test_train: Train):
        """Test transition to MANUAL when stopped."""
        from train_state_manager import TrainStateManager

        test_train.state = TrainState.IDLE
        test_train.current_speed = 0.0
        result = TrainStateManager.can_transition(test_train, TrainState.MANUAL)
        assert result.success is True

    def test_cannot_transition_to_manual_when_moving(self, test_train: Train):
        """Test transition to MANUAL fails when moving."""
        from train_state_manager import TrainStateManager

        test_train.state = TrainState.STANDBY
        test_train.current_speed = 50.0
        result = TrainStateManager.can_transition(test_train, TrainState.MANUAL)
        assert result.success is False
        assert "moving" in result.error.lower()

    def test_cannot_transition_to_manual_from_off(self, test_train: Train):
        """Test transition to MANUAL fails from OFF."""
        from train_state_manager import TrainStateManager

        test_train.state = TrainState.OFF
        result = TrainStateManager.can_transition(test_train, TrainState.MANUAL)
        assert result.success is False

    def test_invalid_transition(self, test_train: Train):
        """Test invalid transition is rejected."""
        from train_state_manager import TrainStateManager

        test_train.state = TrainState.OFF
        result = TrainStateManager.can_transition(test_train, TrainState.RUNNING_MOVING)
        assert result.success is False
        assert "invalid" in result.error.lower()

    def test_transition_updates_state(self, test_train: Train):
        """Test transition() method updates train state."""
        from train_state_manager import TrainStateManager

        test_train.state = TrainState.OFF
        result = TrainStateManager.transition(test_train, TrainState.IDLE)
        assert result.success is True
        assert test_train.state == TrainState.IDLE

    def test_transition_fails_does_not_update_state(self, test_train: Train):
        """Test failed transition does not update state."""
        from train_state_manager import TrainStateManager

        test_train.state = TrainState.OFF
        result = TrainStateManager.transition(test_train, TrainState.RUNNING_MOVING)
        assert result.success is False
        assert test_train.state == TrainState.OFF  # State unchanged

    def test_is_state_moving(self):
        """Test is_state_moving helper."""
        from train_state_manager import TrainStateManager

        assert TrainStateManager.is_state_moving(TrainState.RUNNING_MOVING) is True
        assert TrainStateManager.is_state_moving(TrainState.MANUAL) is True
        assert TrainStateManager.is_state_moving(TrainState.IDLE) is False
        assert TrainStateManager.is_state_moving(TrainState.STANDBY) is False
        assert TrainStateManager.is_state_moving(TrainState.RUNNING_WAITING) is False

    def test_is_state_powered(self):
        """Test is_state_powered helper."""
        from train_state_manager import TrainStateManager

        assert TrainStateManager.is_state_powered(TrainState.OFF) is False
        assert TrainStateManager.is_state_powered(TrainState.IDLE) is True
        assert TrainStateManager.is_state_powered(TrainState.RUNNING_MOVING) is True

    def test_get_state_description(self):
        """Test get_state_description helper."""
        from train_state_manager import TrainStateManager

        desc = TrainStateManager.get_state_description(TrainState.OFF)
        assert "off" in desc.lower()
        assert "no power" in desc.lower()


class TestTrainStateTransitions:
    """Tests for TrainController state transition methods."""

    def test_power_on_transition(self):
        """Test power_on_cmd() transitions OFF -> IDLE."""
        train, controller, network = create_test_train_with_controller(
            state=TrainState.OFF
        )
        result = controller.power_on_cmd()
        assert result.success is True
        assert train.state == TrainState.IDLE
        assert controller.state == TrainState.IDLE

    def test_power_on_fails_when_already_on(self):
        """Test power_on_cmd() fails when already powered."""
        train, controller, network = create_test_train_with_controller(
            state=TrainState.IDLE
        )
        result = controller.power_on_cmd()
        assert result.success is False
        assert train.state == TrainState.IDLE

    def test_power_off_when_stopped(self):
        """Test power_off_cmd() works when stopped."""
        train, controller, network = create_test_train_with_controller(
            state=TrainState.IDLE
        )
        result = controller.power_off_cmd()
        assert result.success is True
        assert train.state == TrainState.OFF
        assert controller.state == TrainState.OFF

    def test_power_off_fails_when_moving(self):
        """Test power_off_cmd() fails when moving."""
        train, controller, network = create_test_train_with_controller(
            state=TrainState.RUNNING_MOVING, current_speed=50.0
        )
        result = controller.power_off_cmd()
        assert result.success is False
        assert train.state == TrainState.RUNNING_MOVING

    def test_set_standby_transition(self):
        """Test set_direction_and_standby() transitions IDLE -> STANDBY."""
        train, controller, network = create_test_train_with_controller(
            state=TrainState.IDLE
        )
        result = controller.set_direction_and_standby(Direction.NORTH)
        assert result is True
        assert train.state == TrainState.STANDBY
        assert train.facing_direction == Direction.NORTH

    def test_set_standby_fails_from_wrong_state(self):
        """Test set_direction_and_standby() fails from non-IDLE state."""
        train, controller, network = create_test_train_with_controller(
            state=TrainState.OFF
        )
        result = controller.set_direction_and_standby(Direction.NORTH)
        assert result is False
        assert train.state == TrainState.OFF

    def test_depart_transition(self):
        """Test depart() transitions STANDBY -> RUNNING_MOVING."""
        train, controller, network = create_test_train_with_controller(
            state=TrainState.STANDBY
        )
        train.facing_direction = Direction.NORTH
        # Use manual mode to test depart transition (not ATO auto-cab logic)
        controller.control_mode = "manual"
        result = controller.depart()
        assert result is True
        assert train.state == TrainState.RUNNING_MOVING

    def test_depart_fails_without_direction(self):
        """Test depart() fails without facing direction."""
        train, controller, network = create_test_train_with_controller(
            state=TrainState.STANDBY
        )
        train.facing_direction = None
        result = controller.depart()
        assert result is False

    def test_depart_fails_from_wrong_state(self):
        """Test depart() fails from non-STANDBY state."""
        train, controller, network = create_test_train_with_controller(
            state=TrainState.IDLE
        )
        train.facing_direction = Direction.NORTH
        result = controller.depart()
        assert result is False

    def test_wait_for_path_transition(self):
        """Test wait_for_path() transitions RUNNING_MOVING -> RUNNING_WAITING."""
        train, controller, network = create_test_train_with_controller(
            state=TrainState.RUNNING_MOVING
        )
        train.target_speed = 60.0
        result = controller.wait_for_path()
        assert result is True
        assert train.state == TrainState.RUNNING_WAITING
        assert train.target_speed == 0.0

    def test_clear_to_move_transition(self):
        """Test clear_to_move() transitions RUNNING_WAITING -> RUNNING_MOVING."""
        train, controller, network = create_test_train_with_controller(
            state=TrainState.RUNNING_WAITING
        )
        result = controller.clear_to_move()
        assert result is True
        assert train.state == TrainState.RUNNING_MOVING

    def test_arrive_at_station_transition(self):
        """Test arrive_at_station() transitions RUNNING_MOVING -> RUNNING_AT_STATION."""
        train, controller, network = create_test_train_with_controller(
            state=TrainState.RUNNING_MOVING
        )
        train.target_speed = 40.0
        result = controller.arrive_at_station()
        assert result is True
        assert train.state == TrainState.RUNNING_AT_STATION
        assert train.target_speed == 0.0

    def test_depart_from_station_with_more_waypoints(self):
        """Test depart_from_station() transitions to STANDBY when more waypoints."""
        train, controller, network = create_test_train_with_controller(
            state=TrainState.RUNNING_AT_STATION
        )
        train.assigned_route_id = "route1"
        result = controller.depart_from_station(has_more_waypoints=True)
        assert result is True
        assert train.state == TrainState.STANDBY
        assert train.assigned_route_id == "route1"  # Route still assigned

    def test_depart_from_station_route_complete(self):
        """Test depart_from_station() transitions to IDLE when route complete."""
        train, controller, network = create_test_train_with_controller(
            state=TrainState.RUNNING_AT_STATION
        )
        train.assigned_route_id = "route1"
        result = controller.depart_from_station(has_more_waypoints=False)
        assert result is True
        assert train.state == TrainState.IDLE
        assert train.assigned_route_id is None  # Route cleared
        assert train.target_speed == 0.0

    def test_emergency_stop_cmd_from_running(self):
        """Test emergency_stop_cmd() from RUNNING_MOVING."""
        train, controller, network = create_test_train_with_controller(
            state=TrainState.RUNNING_MOVING
        )
        train.target_speed = 80.0
        result = controller.emergency_stop_cmd()
        assert result.success is True
        assert controller.eb_active is True
        assert train.emergency_brake is True

    def test_emergency_stop_cmd_always_allowed(self):
        """Test emergency_stop_cmd() works from any state."""
        train, controller, network = create_test_train_with_controller(
            state=TrainState.IDLE
        )
        result = controller.emergency_stop_cmd()
        assert result.success is True
        assert controller.eb_active is True

    def test_enable_manual_from_idle(self):
        """Test enable_manual() from IDLE when stopped."""
        train, controller, network = create_test_train_with_controller(
            state=TrainState.IDLE
        )
        result = controller.enable_manual()
        assert result is True
        assert train.state == TrainState.MANUAL

    def test_enable_manual_fails_when_moving(self):
        """Test enable_manual() fails when moving."""
        train, controller, network = create_test_train_with_controller(
            state=TrainState.STANDBY, current_speed=30.0
        )
        result = controller.enable_manual()
        assert result is False

    def test_enable_manual_fails_from_off(self):
        """Test enable_manual() fails from OFF."""
        train, controller, network = create_test_train_with_controller(
            state=TrainState.OFF
        )
        result = controller.enable_manual()
        assert result is False

    def test_disable_manual_to_idle(self):
        """Test disable_manual() transitions to IDLE when no direction."""
        train, controller, network = create_test_train_with_controller(
            state=TrainState.MANUAL
        )
        train.facing_direction = None
        result = controller.disable_manual()
        assert result is True
        assert train.state == TrainState.IDLE
        assert train.manual_target_speed == 0.0

    def test_disable_manual_to_standby(self):
        """Test disable_manual() transitions to STANDBY when direction set."""
        train, controller, network = create_test_train_with_controller(
            state=TrainState.MANUAL
        )
        train.facing_direction = Direction.EAST
        result = controller.disable_manual()
        assert result is True
        assert train.state == TrainState.STANDBY

    def test_disable_manual_fails_when_moving(self):
        """Test disable_manual() fails when moving."""
        train, controller, network = create_test_train_with_controller(
            state=TrainState.MANUAL, current_speed=40.0
        )
        result = controller.disable_manual()
        assert result is False

    def test_disable_manual_fails_from_wrong_state(self):
        """Test disable_manual() fails from non-MANUAL state."""
        train, controller, network = create_test_train_with_controller(
            state=TrainState.IDLE
        )
        result = controller.disable_manual()
        assert result is False

    def test_set_direction_and_standby(self):
        """Test set_direction_and_standby() convenience method."""
        train, controller, network = create_test_train_with_controller(
            state=TrainState.IDLE
        )
        result = controller.set_direction_and_standby(Direction.SOUTH)
        assert result is True
        assert train.state == TrainState.STANDBY
        assert train.facing_direction == Direction.SOUTH

    def test_set_manual_speed(self):
        """Test set_manual_speed() in MANUAL mode - uses Train method directly."""
        train, controller, network = create_test_train_with_controller(
            state=TrainState.MANUAL
        )
        # Note: set_manual_speed is on Train, not controller
        result = train.set_manual_speed(50.0)
        assert result is True
        assert train.manual_target_speed == 50.0

    def test_set_manual_speed_clamps_to_max(self):
        """Test set_manual_speed() clamps to max_speed."""
        train, controller, network = create_test_train_with_controller(
            state=TrainState.MANUAL, max_speed=100.0
        )
        result = train.set_manual_speed(150.0)
        assert result is True
        assert train.manual_target_speed == 100.0

    def test_set_manual_speed_clamps_to_zero(self):
        """Test set_manual_speed() clamps negative to 0."""
        train, controller, network = create_test_train_with_controller(
            state=TrainState.MANUAL
        )
        result = train.set_manual_speed(-10.0)
        assert result is True
        assert train.manual_target_speed == 0.0

    def test_set_manual_speed_fails_when_not_manual(self):
        """Test set_manual_speed() fails when not in MANUAL mode."""
        train, controller, network = create_test_train_with_controller(
            state=TrainState.IDLE
        )
        result = train.set_manual_speed(50.0)
        assert result is False


class TestStateBasedVoltageOutput:
    """Tests for state-based voltage output in BlockPowerController."""

    def test_voltage_for_off_state(self):
        """Test voltage is 0 for OFF state."""
        from block_power import BlockPowerController
        from train import Train

        train = Train(train_id="T001")
        train.state = TrainState.OFF

        controller = BlockPowerController()
        voltage = controller._calculate_voltage_for_train(train)
        assert voltage == 0.0

    def test_voltage_for_idle_state(self):
        """Test voltage is lighting voltage for IDLE state."""
        from block_power import BlockPowerController
        from train import Train

        train = Train(train_id="T001")
        train.state = TrainState.IDLE

        controller = BlockPowerController()
        controller.default_lighting_voltage = 2.5
        voltage = controller._calculate_voltage_for_train(train)
        assert voltage == 2.5

    def test_voltage_for_standby_state(self):
        """Test voltage is lighting voltage for STANDBY state."""
        from block_power import BlockPowerController
        from train import Train

        train = Train(train_id="T001")
        train.state = TrainState.STANDBY

        controller = BlockPowerController()
        controller.default_lighting_voltage = 2.5
        voltage = controller._calculate_voltage_for_train(train)
        assert voltage == 2.5

    def test_voltage_for_running_waiting_state(self):
        """Test voltage is lighting voltage for RUNNING_WAITING state."""
        from block_power import BlockPowerController
        from train import Train

        train = Train(train_id="T001")
        train.state = TrainState.RUNNING_WAITING

        controller = BlockPowerController()
        voltage = controller._calculate_voltage_for_train(train)
        assert voltage == controller.default_lighting_voltage

    def test_voltage_for_running_at_station_state(self):
        """Test voltage is lighting voltage for RUNNING_AT_STATION state."""
        from block_power import BlockPowerController
        from train import Train

        train = Train(train_id="T001")
        train.state = TrainState.RUNNING_AT_STATION

        controller = BlockPowerController()
        voltage = controller._calculate_voltage_for_train(train)
        assert voltage == controller.default_lighting_voltage

    def test_voltage_for_running_moving_state_uses_speed(self):
        """Test voltage uses target_speed for RUNNING_MOVING state."""
        from block_power import BlockPowerController
        from train import Train
        from speed_profile import SpeedProfile

        train = Train(train_id="T001")
        train.state = TrainState.RUNNING_MOVING
        train.target_speed = 60.0

        profile = SpeedProfile(
            profile_id="test",
            name="Test",
            speed_voltage_curve=[(0, 0), (60, 8), (120, 12)],
        )

        controller = BlockPowerController()
        voltage = controller._calculate_voltage_for_train(train, profile)
        assert voltage == 8.0

    def test_voltage_for_manual_state_uses_manual_speed(self):
        """Test voltage uses manual_target_speed for MANUAL state."""
        from block_power import BlockPowerController
        from train import Train
        from speed_profile import SpeedProfile

        train = Train(train_id="T001")
        train.state = TrainState.MANUAL
        train.manual_target_speed = 40.0

        profile = SpeedProfile(
            profile_id="test",
            name="Test",
            speed_voltage_curve=[(0, 0), (40, 6), (120, 12)],
        )

        controller = BlockPowerController()
        voltage = controller._calculate_voltage_for_train(train, profile)
        assert voltage == 6.0


class TestCompleteStateFlows:
    """Integration tests for complete state transition flows via TrainController."""

    def test_normal_startup_to_running_flow(self):
        """Test complete flow: OFF -> IDLE -> STANDBY -> RUNNING_MOVING."""
        train, controller, network = create_test_train_with_controller(
            state=TrainState.OFF
        )

        # Power on
        result = controller.power_on_cmd()
        assert result.success is True
        assert train.state == TrainState.IDLE

        # Set direction and standby
        assert controller.set_direction_and_standby(Direction.NORTH) is True
        assert train.state == TrainState.STANDBY
        assert train.facing_direction == Direction.NORTH

        # Depart (manual mode to bypass ATO auto-cab selection)
        controller.control_mode = "manual"
        assert controller.depart() is True
        assert train.state == TrainState.RUNNING_MOVING

    def test_running_to_station_to_complete_flow(self):
        """Test complete flow through station arrival and route completion."""
        train, controller, network = create_test_train_with_controller(
            state=TrainState.RUNNING_MOVING
        )
        train.assigned_route_id = "route1"

        # Arrive at station
        assert controller.arrive_at_station() is True
        assert train.state == TrainState.RUNNING_AT_STATION
        assert train.target_speed == 0.0

        # Depart with more waypoints
        assert controller.depart_from_station(has_more_waypoints=True) is True
        assert train.state == TrainState.STANDBY
        assert train.assigned_route_id == "route1"

        # Simulate moving and arriving at final station
        # Need to set controller state too for proper flow
        train.facing_direction = Direction.NORTH
        # Use manual mode to test depart (not ATO auto-cab logic)
        controller.control_mode = "manual"
        controller.depart()
        assert train.state == TrainState.RUNNING_MOVING

        assert controller.arrive_at_station() is True
        assert train.state == TrainState.RUNNING_AT_STATION

        # Route complete
        assert controller.depart_from_station(has_more_waypoints=False) is True
        assert train.state == TrainState.IDLE
        assert train.assigned_route_id is None

    def test_manual_mode_flow(self):
        """Test complete manual mode flow."""
        train, controller, network = create_test_train_with_controller(
            state=TrainState.IDLE
        )

        # Enable manual
        assert controller.enable_manual() is True
        assert train.state == TrainState.MANUAL

        # Set manual speed (still on Train - not moved to controller)
        assert train.set_manual_speed(50.0) is True
        assert train.manual_target_speed == 50.0

        # Cannot disable while "moving" (simulated)
        train.current_speed = 50.0
        assert controller.disable_manual() is False
        assert train.state == TrainState.MANUAL

        # Stop and disable
        train.current_speed = 0.0
        assert controller.disable_manual() is True
        assert train.state == TrainState.IDLE
        assert train.manual_target_speed == 0.0

    def test_emergency_stop_flow(self):
        """Test emergency stop flow via TrainController."""
        train, controller, network = create_test_train_with_controller(
            state=TrainState.RUNNING_MOVING, current_speed=80.0
        )
        train.target_speed = 100.0

        # Emergency stop
        result = controller.emergency_stop_cmd()
        assert result.success is True
        assert controller.eb_active is True
        assert train.emergency_brake is True
        # Emergency stop sets handle to EB
        assert controller.handle_position.value == "EB"

        # Cannot release emergency until stopped and handle in N/B
        train.current_speed = 0.0  # Simulate stopping
        # Handle is still EB, need to set to N first
        controller.handle_position = HandlePosition.N
        result = controller.release_emergency_cmd()
        assert result.success is True
        assert controller.eb_active is False

    def test_shutdown_flow(self):
        """Test complete shutdown flow."""
        train, controller, network = create_test_train_with_controller(
            state=TrainState.RUNNING_MOVING, current_speed=50.0
        )

        # Cannot power off while moving
        result = controller.power_off_cmd()
        assert result.success is False
        assert train.state == TrainState.RUNNING_MOVING

        # Stop first - simulate stopping
        train.current_speed = 0.0
        controller.state = TrainState.IDLE
        train.state = TrainState.IDLE

        # Now can power off
        result = controller.power_off_cmd()
        assert result.success is True
        assert train.state == TrainState.OFF
        assert train.target_speed == 0.0


class TestEmergencyResetButtonBugFix:
    """
    Comprehensive tests for emergency reset button bug fixed in 2026-01-24.

    BUG: Emergency stop forces the handle to EB and TrainController.set_handle
    rejects all handle changes while emergency braking is active. The UI
    attempts to reset by sending set_handle('N'), which fails and leaves
    the train stuck in emergency stop.

    FIX: Allow release when the train is stopped, even if the handle is EB,
    and let set_handle(N/B) trigger emergency release instead of rejecting it.
    """

    def test_release_allows_eb_handle_when_stopped(self):
        """Release should succeed even when handle remains EB."""
        train, controller, _ = create_test_train_with_controller(
            state=TrainState.RUNNING_MOVING, current_speed=40.0
        )

        result = controller.emergency_stop_cmd()
        assert result.success is True

        train.current_speed = 0.0
        result = controller.release_emergency_cmd()

        assert result.success is True
        assert controller.eb_active is False
        assert train.emergency_brake is False

    def test_set_handle_neutral_releases_emergency(self):
        """UI reset via handle neutral should release emergency stop."""
        train, controller, _ = create_test_train_with_controller(
            state=TrainState.RUNNING_MOVING, current_speed=30.0
        )

        result = controller.emergency_stop_cmd()
        assert result.success is True

        train.current_speed = 0.0
        result = controller.set_handle(HandlePosition.N)

        assert result.success is True
        assert controller.eb_active is False
        assert train.emergency_brake is False

    def test_set_handle_release_fails_when_moving(self):
        """Release should still be blocked while the train is moving."""
        train, controller, _ = create_test_train_with_controller(
            state=TrainState.RUNNING_MOVING, current_speed=30.0
        )

        result = controller.emergency_stop_cmd()
        assert result.success is True

        result = controller.set_handle(HandlePosition.N)

        assert result.success is False
        assert controller.eb_active is True

    def test_set_handle_release_rejects_power_position(self):
        """Handle release should not accept power positions."""
        train, controller, _ = create_test_train_with_controller(
            state=TrainState.RUNNING_MOVING, current_speed=0.0
        )

        result = controller.emergency_stop_cmd()
        assert result.success is True

        result = controller.set_handle(HandlePosition.P1)

        assert result.success is False
        assert controller.eb_active is True
