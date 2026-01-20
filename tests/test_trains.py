"""
Train Tests - Train placement, movement, and occupancy

Run with: pytest tests/test_trains.py -v
"""

import pytest

from track import TrackSegment, TRACK_NS, TRACK_EW
from network import RailwayNetwork
from block import Block
from enums import BlockState, Direction, TrainState, HandlePosition
from train import Train, TrainCar, TrainPosition
from dispatcher import TrainDispatcher
from simulation import SimulationController
from train_controller import TrainController, create_controller_for_train


@pytest.fixture
def network():
    return RailwayNetwork()


@pytest.fixture
def straight_ew_track(network):
    for x in range(5):
        track = TrackSegment(x=x, y=0, track_bits=TRACK_EW)
        network.add_track(track)
    return network


class TestTrainDispatcher:
    """Tests for TrainDispatcher class"""

    def test_dispatcher_creation(self, network):
        dispatcher = TrainDispatcher(network)
        assert dispatcher is not None
        assert dispatcher.network == network


class TestSimulationController:
    """Tests for SimulationController class"""

    def test_simulation_creation(self, network):
        simulation = SimulationController(network)
        assert simulation is not None
        assert simulation.network == network
        assert simulation.power_controller is not None
        assert simulation.route_executor is not None
        assert simulation.speed_profile_registry is not None


class TestTrainManualControlBugFixes:
    """Tests for train manual control and ATC fallback fixes via TrainController."""

    def test_clear_direction_from_standby(self):
        network = RailwayNetwork()
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_01", consist=[car], max_speed=60.0)
        train.state = TrainState.STANDBY
        train.facing_direction = Direction.EAST
        train.travel_direction = "reverse"
        train.manual_target_speed = 20.0
        train.target_speed = 15.0
        network.add_train(train)
        controller = create_controller_for_train("TRAIN_01", network)
        controller.power_on = True
        controller.state = TrainState.STANDBY

        assert controller.clear_direction() is True
        assert train.state == TrainState.IDLE
        assert train.facing_direction is None
        assert train.travel_direction == "forward"
        assert train.manual_target_speed == 0.0
        assert train.target_speed == 0.0

    def test_clear_direction_rejects_moving(self):
        network = RailwayNetwork()
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_02", consist=[car], max_speed=60.0)
        train.state = TrainState.STANDBY
        train.facing_direction = Direction.NORTH
        train.current_speed = 5.0
        network.add_train(train)
        controller = create_controller_for_train("TRAIN_02", network)
        controller.power_on = True
        controller.state = TrainState.STANDBY

        assert controller.clear_direction() is False
        assert train.state == TrainState.STANDBY

    def test_manual_speed_requires_manual_and_clamps(self):
        """Test set_manual_speed - still on Train (not controller)."""
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_03", consist=[car], max_speed=40.0)
        assert train.set_manual_speed(30.0) is False
        train.state = TrainState.MANUAL
        assert train.set_manual_speed(60.0) is True
        assert train.manual_target_speed == 40.0

    def test_travel_direction_requires_manual(self):
        """Test set_travel_direction via TrainController."""
        network = RailwayNetwork()
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_04", consist=[car], max_speed=60.0)
        network.add_train(train)
        controller = create_controller_for_train("TRAIN_04", network)

        # Not in manual mode - fails
        result = controller.set_travel_direction("reverse")
        assert result.success is False

        # Enable manual mode first
        controller.power_on = True
        train.state = TrainState.MANUAL
        controller.state = TrainState.MANUAL
        controller.control_mode = "manual"

        result = controller.set_travel_direction("reverse")
        assert result.success is True
        assert train.travel_direction == "reverse"


class TestTrainPlacementOccupancy:
    """Tests for train placement and occupancy."""

    def test_train_update_occupancy_sets_block_occupied(self, network):
        track = TrackSegment(x=0, y=0, track_bits=TRACK_EW)
        network.add_track(track)
        block = Block(block_id="BLK_001", name="Test Block")
        network.blocks["BLK_001"] = block
        block.tiles = [(0, 0)]
        track.block_section_id = "BLK_001"
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_01", consist=[car], max_speed=60.0)
        train.set_position((0, 0), Direction.EAST)
        assert block.state == BlockState.FREE
        network.add_train(train)
        train.update_occupancy(network)
        assert block.state == BlockState.OCCUPIED
        assert block.occupied_by == "TRAIN_01"

    def test_train_update_occupancy_with_multiple_tiles(self, network):
        for x in range(3):
            track = TrackSegment(x=x, y=0, track_bits=TRACK_EW)
            network.add_track(track)
            track.block_section_id = "BLK_001"
        block = Block(block_id="BLK_001", name="Long Block")
        network.blocks["BLK_001"] = block
        block.tiles = [(0, 0), (1, 0), (2, 0)]
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_03", consist=[car], max_speed=60.0)
        train.set_position((1, 0), Direction.EAST)
        network.add_train(train)
        train.update_occupancy(network)
        assert block.state == BlockState.OCCUPIED
        assert block.occupied_by == "TRAIN_03"


class TestTrainPropertyUpdate:
    """Tests for train property updates."""

    def test_train_name_update(self, network):
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_01", consist=[car], max_speed=60.0)
        train.name = "Original Name"
        network.add_train(train)
        train.name = "Updated Name"
        assert network.trains["TRAIN_01"].name == "Updated Name"

    def test_train_max_speed_update(self, network):
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_01", consist=[car], max_speed=60.0)
        network.add_train(train)
        train.max_speed = 120.0
        assert network.trains["TRAIN_01"].max_speed == 120.0

    def test_train_length_update(self, network):
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_01", consist=[car], max_speed=60.0)
        network.add_train(train)
        assert train.length_cm == 20.0
        train.consist.append(TrainCar(car_id="CAR_02", car_type="FREIGHT", length=30.0))
        assert train.length_cm == 50.0


class TestTrainPlacement:
    """Tests for proper train placement based on direction and length."""

    def test_place_train_head_at_exit_edge(self, network):
        for x in range(3):
            network.add_track(TrackSegment(x, 0, track_bits=TRACK_EW))
        network.create_block_with_tracks(
            "BLOCK_1", [(0, 0), (1, 0), (2, 0)], length_cm=150.0
        )
        car = TrainCar("CAR_1", "LOCOMOTIVE", length=30.0)
        train = Train(train_id="T1", consist=[car])
        network.add_train(train)
        final_pos = network.place_train(train, 0, 0, Direction.EAST)
        assert train.current_pos == (0, 0)
        assert train.current_direction == Direction.EAST
        tile_length_cm = network._get_tile_length_cm(0, 0)
        assert train.distance_on_track == tile_length_cm

    def test_place_train_no_track_raises_error(self, network):
        car = TrainCar("CAR_1", "LOCOMOTIVE", length=30.0)
        train = Train(train_id="T1", consist=[car])
        network.add_train(train)
        with pytest.raises(ValueError):
            network.place_train(train, 0, 0, Direction.EAST)


class TestTrainPosition:
    """Tests for TrainPosition dataclass and basic functionality."""

    def test_train_position_creation(self):
        pos = TrainPosition(
            end_a_tile=(1, 0),
            end_a_distance_cm=50.0,
            end_a_entry_edge="W",
            end_b_tile=(0, 0),
            end_b_distance_cm=10.0,
            end_b_entry_edge="W",
            leading_end="a",
            facing_direction=Direction.EAST,
            occupied_tiles=[(0, 0), (1, 0)],
            occupied_blocks=["BLOCK_1"],
        )
        assert pos.end_a_tile == (1, 0)
        assert pos.end_b_tile == (0, 0)
        assert pos.leading_end == "a"

    def test_train_position_head_tail_properties(self):
        pos = TrainPosition(
            end_a_tile=(1, 0),
            end_a_distance_cm=50.0,
            end_a_entry_edge="W",
            end_b_tile=(0, 0),
            end_b_distance_cm=10.0,
            end_b_entry_edge="W",
            leading_end="a",
            facing_direction=Direction.EAST,
        )
        assert pos.head_tile == (1, 0)
        assert pos.head_distance_cm == 50.0
        assert pos.tail_tile == (0, 0)
        assert pos.tail_distance_cm == 10.0

    def test_train_position_reverse(self):
        pos = TrainPosition(
            end_a_tile=(1, 0),
            end_a_distance_cm=50.0,
            end_a_entry_edge="W",
            end_b_tile=(0, 0),
            end_b_distance_cm=10.0,
            end_b_entry_edge="W",
            leading_end="a",
            facing_direction=Direction.EAST,
        )
        pos.reverse()
        assert pos.leading_end == "b"
        assert pos.facing_direction == Direction.WEST
        assert pos.head_tile == (0, 0)
        assert pos.tail_tile == (1, 0)

    def test_train_position_to_dict(self):
        pos = TrainPosition(
            end_a_tile=(1, 0),
            end_a_distance_cm=50.0,
            end_a_entry_edge="W",
            end_b_tile=(0, 0),
            end_b_distance_cm=10.0,
            end_b_entry_edge="W",
            leading_end="a",
            facing_direction=Direction.EAST,
            occupied_tiles=[(0, 0), (1, 0)],
            occupied_blocks=["BLOCK_1"],
        )
        data = pos.to_dict()
        assert data["end_a"]["tile"] == [1, 0]
        assert data["end_a"]["distance_cm"] == 50.0
        assert data["leading_end"] == "a"
        assert data["facing_direction"] == "EAST"

    def test_train_position_from_dict(self):
        data = {
            "end_a": {"tile": [1, 0], "distance_cm": 50.0, "entry_edge": "W"},
            "end_b": {"tile": [0, 0], "distance_cm": 10.0, "entry_edge": "W"},
            "leading_end": "a",
            "facing_direction": "EAST",
            "occupied_tiles": [[0, 0], [1, 0]],
            "occupied_blocks": ["BLOCK_1"],
        }
        pos = TrainPosition.from_dict(data)
        assert pos is not None
        assert pos.end_a_tile == (1, 0)
        assert pos.facing_direction == Direction.EAST


class TestTrainPlacementValidation:
    """Tests for train placement with length validation."""

    def test_40cm_train_on_150cm_block_fits(self, network):
        for x in range(3):
            network.add_track(TrackSegment(x, 0, track_bits=TRACK_EW))
        network.create_block_with_tracks(
            "BLOCK_1", [(0, 0), (1, 0), (2, 0)], length_cm=150.0
        )
        car = TrainCar("CAR_1", "LOCOMOTIVE", length=40.0)
        train = Train(train_id="T1", consist=[car])
        network.add_train(train)
        network.place_train(train, 1, 0, Direction.EAST)
        assert train.position is not None
        assert train.position.head_tile == (1, 0)

    def test_80cm_train_spans_two_tiles(self, network):
        for x in range(3):
            network.add_track(TrackSegment(x, 0, track_bits=TRACK_EW))
        network.create_block_with_tracks(
            "BLOCK_1", [(0, 0), (1, 0), (2, 0)], length_cm=150.0
        )
        car = TrainCar("CAR_1", "LOCOMOTIVE", length=80.0)
        train = Train(train_id="T1", consist=[car])
        network.add_train(train)
        network.place_train(train, 1, 0, Direction.EAST)
        assert train.position.head_tile == (1, 0)
        assert train.position.tail_tile == (0, 0)
        assert len(train.position.occupied_tiles) == 2

    def test_train_too_long_raises_error(self, network):
        for x in range(2):
            network.add_track(TrackSegment(x, 0, track_bits=TRACK_EW))
        network.create_block_with_tracks("BLOCK_1", [(0, 0), (1, 0)], length_cm=100.0)
        car = TrainCar("CAR_1", "LOCOMOTIVE", length=120.0)
        train = Train(train_id="T1", consist=[car])
        network.add_train(train)
        with pytest.raises(ValueError, match="no track at"):
            network.place_train(train, 1, 0, Direction.EAST)


class TestTrainOccupiedTilesRich:
    """Tests for get_occupied_tiles_rich method."""

    def test_rich_tiles_single_tile_train(self, network):
        for x in range(3):
            network.add_track(TrackSegment(x, 0, track_bits=TRACK_EW))
        network.create_block_with_tracks(
            "BLOCK_1", [(0, 0), (1, 0), (2, 0)], length_cm=150.0
        )
        car = TrainCar("CAR_1", "LOCOMOTIVE", length=40.0)
        train = Train(train_id="T1", consist=[car])
        network.add_train(train)
        network.place_train(train, 1, 0, Direction.EAST)
        rich = network.get_occupied_tiles_rich(train)
        assert len(rich) == 1
        assert rich[0]["tile"] == (1, 0)
        assert rich[0]["block_id"] == "BLOCK_1"


class TestTrainReverse:
    """Tests for train reversal."""

    def test_reverse_train_swaps_leading_end(self, network):
        for x in range(3):
            network.add_track(TrackSegment(x, 0, track_bits=TRACK_EW))
        network.create_block_with_tracks(
            "BLOCK_1", [(0, 0), (1, 0), (2, 0)], length_cm=150.0
        )
        car = TrainCar("CAR_1", "LOCOMOTIVE", length=40.0)
        train = Train(train_id="T1", consist=[car])
        network.add_train(train)
        network.place_train(train, 1, 0, Direction.EAST)
        assert train.position.leading_end == "a"
        assert train.position.facing_direction == Direction.EAST
        old_head = train.position.head_tile
        network.reverse_train(train)
        assert train.position.leading_end == "b"
        assert train.position.facing_direction == Direction.WEST


class TestRaceConditionFix:
    """Tests for race condition fixes."""

    def test_block_manager_prevents_premature_release(self, network):
        block = network.create_block("B1", "Block 1")
        network.block_manager.occupy_block("B1", "TRAIN_1")
        assert block.state == BlockState.OCCUPIED
        network.block_manager.set_block_to_caution("B1")
        assert block.state == BlockState.CAUTION
        assert block.state != BlockState.FREE

    def test_simulation_uses_new_architecture(self):
        """Verify SimulationController uses TrainController + TrainPhysics architecture."""
        import simulation

        # Verify SimulationController has the new architecture attributes
        assert hasattr(simulation.SimulationController, "_get_or_create_controller")
        assert hasattr(simulation.SimulationController, "_get_or_create_physics")

        # Verify it no longer has the old _advance_train_track method
        assert not hasattr(simulation.SimulationController, "_advance_train_track")
        assert not hasattr(simulation.SimulationController, "_update_speed")

    def test_controller_state_is_source_of_truth(self):
        """
        Test that TrainController state is the source of truth, not train.state.

        BUG: simulation.py:94 was copying train.state -> controller.state BEFORE tick,
        then controller.tick() modified controller.state, leaving train.state stale.

        FIX: Controller state should be synced TO train AFTER tick, not FROM train BEFORE.

        This test sets train.state to one value and controller.state to another,
        then verifies that after update(), train.state matches controller.state
        (proving controller is the authority).
        """
        network = RailwayNetwork()
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_STATE_TEST", consist=[car], max_speed=60.0)
        train.state = TrainState.IDLE
        network.add_train(train)

        sim = SimulationController(network)

        # First update to initialize controller
        sim.update(0.1)

        controller = sim.controllers[train.train_id]

        # Now manually set controller to different state than train
        # (simulating what would happen if controller.tick() changes state)
        train.state = TrainState.IDLE
        controller.state = TrainState.STANDBY

        # After another update, train should reflect controller's state
        sim.update(0.1)

        # train.state should now match controller.state (controller wins)
        assert train.state == controller.state

    def test_controller_transitions_to_waiting_when_stopped(self):
        """
        Test that controller transitions from RUNNING_MOVING to RUNNING_WAITING when stopped.

        BUG: train_controller.py:263-267 doesn't auto-transition from RUNNING_MOVING
        to RUNNING_WAITING when train is stopped (speed = 0).

        FIX: Add check in controller.tick() to transition RUNNING_MOVING -> RUNNING_WAITING
        when current_speed <= STOPPED_SPEED_THRESHOLD.
        """
        network = RailwayNetwork()
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_TRANSITION_TEST", consist=[car], max_speed=60.0)
        train.state = TrainState.RUNNING_MOVING
        train.current_speed = 0.0  # Train is stopped
        network.add_train(train)

        sim = SimulationController(network)
        controller = sim._get_or_create_controller(train)
        controller.power_on = True
        controller.state = TrainState.RUNNING_MOVING

        # Tick controller - should detect train is stopped and transition to WAITING
        sim.update(0.1)

        # Controller should have transitioned to RUNNING_WAITING (train is stopped)
        assert controller.state == TrainState.RUNNING_WAITING

    def test_block_release_uses_caution_state(self):
        """
        Test that TrainPhysics releases blocks through CAUTION state, not directly to FREE.

        BUG: train_physics.py:607 uses clear_block() which skips CAUTION state.

        FIX: Should use set_block_to_caution() for proper safety transition.
        Block state machine: OCCUPIED -> CAUTION -> FREE (not OCCUPIED -> FREE).
        """
        network = RailwayNetwork()

        # Create a block
        block = network.create_block("B1", "Block 1")

        # Occupy the block
        network.block_manager.occupy_block("B1", "TRAIN_1")
        assert block.state == BlockState.OCCUPIED

        # Create train
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=100.0)
        train = Train(train_id="TRAIN_1", consist=[car], max_speed=60.0)
        network.add_train(train)

        # Create simulation and physics
        sim = SimulationController(network)
        physics = sim._get_or_create_physics(train)

        # Simulate train leaving block: was occupying B1, now occupies nothing
        physics._last_occupied_blocks = {"B1"}
        train.position = None  # Train has no position (simulates having left)

        # Trigger block occupancy update
        physics._update_block_occupancy(train)

        # Block should be in CAUTION state, not FREE
        assert block.state == BlockState.CAUTION, (
            f"Expected CAUTION but got {block.state}"
        )

    def test_friction_stops_train_when_target_speed_zero(self):
        """
        Test that friction decelerates train to stop even if handle is P1 but target_speed is 0.

        BUG: train_physics.py:199-202 only applies friction when handle=N,
        but if target_speed=0 and handle=P1, train incorrectly accelerates.

        FIX: When target_speed=0, always decelerate regardless of handle position.
        """
        network = RailwayNetwork()
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=100.0)
        train = Train(train_id="TRAIN_FRICTION_TEST", consist=[car], max_speed=60.0)
        train.current_speed = 10.0  # Train is moving
        network.add_train(train)

        sim = SimulationController(network)
        controller = sim._get_or_create_controller(train)
        physics = sim._get_or_create_physics(train)

        # Set controller state: power on, but target speed is 0 (should stop)
        controller.power_on = True
        controller.handle_position = HandlePosition.P1  # Power handle
        controller.target_speed = 0.0  # But target is to stop

        initial_speed = train.current_speed

        # Tick physics - train should decelerate, not accelerate
        physics.tick(0.1)

        # Train should have slowed down (friction/braking), not sped up
        assert train.current_speed < initial_speed, (
            f"Train should decelerate when target_speed=0, "
            f"but speed went from {initial_speed} to {train.current_speed}"
        )

    def test_no_duplicate_controller_instances(self):
        """
        Test that RouteExecutor and TrainDispatcher use the same TrainController
        instances as SimulationController, not creating duplicates.

        BUG: route_executor.py:86-92 and dispatcher.py:54-60 create separate
        controller instances, causing desynchronization.

        FIX: All components should get controllers from SimulationController
        (single source of truth).
        """
        network = RailwayNetwork()
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=100.0)
        train = Train(train_id="TRAIN_SINGLETON_TEST", consist=[car], max_speed=60.0)
        network.add_train(train)

        sim = SimulationController(network)

        # Get controller from SimulationController
        sim_controller = sim._get_or_create_controller(train)

        # RouteExecutor should use the same controller instance
        route_executor_controller = sim.route_executor._get_controller(train)

        # They should be the exact same object (not copies)
        assert sim_controller is route_executor_controller, (
            "RouteExecutor created a duplicate controller instead of using "
            "the SimulationController's instance"
        )


if __name__ == "__main__":
    print("Run tests with: pytest tests/test_trains.py -v")
