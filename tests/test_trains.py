"""
Train Tests - Train placement, movement, and occupancy

Run with: pytest tests/test_trains.py -v
"""

import logging
import pytest

from track import TrackSegment, TRACK_NS, TRACK_EW, TRACK_NW
from network import RailwayNetwork
from block import Block
from enums import BlockState, Direction, TrainState, HandlePosition
from train import Train, TrainCar, TrainPosition
from dispatcher import TrainDispatcher
from simulation import SimulationController
from train_controller import TrainController, create_controller_for_train
from switch import Switch


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


class TestCabSwitchEndPositionsBugFix:
    """
    Comprehensive tests for cab switching end position swaps (2026-01-22).

    BUG: TrainController.set_cab() swapped end_a/end_b positions when switching
    cabs, causing occupied_tiles to stay in the old order. This inverted the
    renderer's head/tail selection when using path_tiles + leading_end.

    FIX: Switching cabs only flips leading_end and facing_direction. Physical
    end_a/end_b positions and occupied_tiles ordering (end_a -> end_b) remain
    unchanged.
    """

    def _setup_two_tile_train(self, network):
        for x in range(3):
            network.add_track(TrackSegment(x, 0, track_bits=TRACK_EW))

        car = TrainCar("CAR_1", "LOCOMOTIVE", length=80.0)
        train = Train(train_id="CAB_SWAP_TEST", consist=[car])
        network.add_train(train)
        network.place_train(train, 1, 0, Direction.EAST)

        controller = create_controller_for_train(train.train_id, network)
        controller.power_on = True

        return train, controller

    def test_cab_switch_keeps_end_positions(self, network):
        train, controller = self._setup_two_tile_train(network)

        controller.set_cab("A")
        original_end_a = train.position.end_a_tile
        original_end_b = train.position.end_b_tile
        original_entry_a = train.position.end_a_entry_edge
        original_entry_b = train.position.end_b_entry_edge

        controller.set_cab("B")

        assert train.position.leading_end == "b"
        assert train.position.end_a_tile == original_end_a
        assert train.position.end_b_tile == original_end_b
        assert train.position.end_a_entry_edge == original_entry_a
        assert train.position.end_b_entry_edge == original_entry_b

    def test_cab_switch_preserves_occupied_tiles_order(self, network):
        train, controller = self._setup_two_tile_train(network)

        controller.set_cab("A")
        original_occupied = train.position.occupied_tiles.copy()

        controller.set_cab("B")

        assert train.position.occupied_tiles == original_occupied
        assert train.position.occupied_tiles[0] == train.position.end_a_tile
        assert train.position.occupied_tiles[-1] == train.position.end_b_tile

    def test_cab_switch_renderer_serialization_order(self, network):
        train, controller = self._setup_two_tile_train(network)

        controller.set_cab("A")
        original_end_a = train.position.end_a_tile
        original_end_b = train.position.end_b_tile

        controller.set_cab("B")
        data = train.to_dict(network)

        assert data["leading_end"] == "b"
        assert data["path_tiles"][0] == list(original_end_a)
        assert data["path_tiles"][-1] == list(original_end_b)
        assert data["end_a_index"] == 0
        assert data["end_b_index"] == len(data["path_tiles"]) - 1


class TestTrainPhysicsOccupiedTilesOrderBugFix:
    """
    Comprehensive tests for occupied_tiles ordering in TrainPhysics (2026-01-22).

    BUG: TrainPhysics._update_occupied_tiles() always stored tiles from tail to
    head, even when leading_end='a'. This inverted end_a/end_b ordering, causing
    renderer head/tail swaps when the train spans multiple tiles.

    FIX: occupied_tiles stays ordered from end_a -> end_b regardless of
    leading_end.
    """

    def _setup_two_tile_train(self, network):
        for x in range(3):
            network.add_track(TrackSegment(x, 0, track_bits=TRACK_EW))

        car = TrainCar("CAR_1", "LOCOMOTIVE", length=80.0)
        train = Train(train_id="OCCUPIED_TILE_ORDER", consist=[car])
        network.add_train(train)
        network.place_train(train, 1, 0, Direction.EAST)

        return train

    def test_occupied_tiles_order_leading_end_a(self, network):
        train = self._setup_two_tile_train(network)

        from train_physics import TrainPhysics

        physics = TrainPhysics(train_id=train.train_id, network=network)
        physics._move_train(train, 1.0)

        assert train.position.leading_end == "a"
        assert train.position.occupied_tiles[0] == train.position.end_a_tile
        assert train.position.occupied_tiles[-1] == train.position.end_b_tile

    def test_renderer_path_tiles_order_leading_end_a(self, network):
        train = self._setup_two_tile_train(network)

        from train_physics import TrainPhysics

        physics = TrainPhysics(train_id=train.train_id, network=network)
        physics._move_train(train, 1.0)

        data = train.to_dict(network)

        assert data["path_tiles"][0] == list(train.position.end_a_tile)
        assert data["path_tiles"][-1] == list(train.position.end_b_tile)


class TestTrainPhysicsTailPositionBugFix:
    """
    Comprehensive tests for incorrect tail distance/entry edge (2026-01-21).

    BUG: TrainPhysics._update_tail_position produced tail distances larger than
    tile length (e.g., 61cm in a 50cm tile) and flipped the tail entry edge when
    the head advanced into the next tile.

    FIX: Tail distance stays within the tile length and preserves the correct
    entry edge when tracing backward across tiles.
    """

    def test_tail_distance_within_tile_after_advance(self, network):
        for x in range(3):
            network.add_track(TrackSegment(x, 0, track_bits=TRACK_EW))
        network.create_block_with_tracks(
            "BLOCK_1", [(0, 0), (1, 0), (2, 0)], length_cm=150.0
        )
        car = TrainCar("CAR_1", "LOCOMOTIVE", length=40.0)
        train = Train(train_id="T1", consist=[car])
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)

        from train_physics import TrainPhysics

        physics = TrainPhysics(train_id=train.train_id, network=network)
        physics._move_train(train, 1.0)

        assert train.position.head_tile == (1, 0)
        assert train.position.tail_tile == (0, 0)
        assert train.position.tail_entry_edge == "W"
        assert train.position.tail_distance_cm == pytest.approx(11.0)


class TestZeroLengthSwitchTraversal:
    """Tests for moving across zero-length switch tiles."""

    def test_train_advances_across_zero_length_switch(self, network):
        network.add_track(TrackSegment(0, 0, track_bits=TRACK_EW))
        switch_track = TrackSegment(1, 0, track_bits=TRACK_EW | TRACK_NW)
        switch_track.switch_state = 0
        network.add_track(switch_track)
        network.add_track(TrackSegment(2, 0, track_bits=TRACK_EW))
        switch = Switch(switch_id="SW_1_0", x=1, y=0)
        network.add_switch(switch)
        switch_track.switch_id = switch.switch_id

        car = TrainCar("CAR_1", "LOCOMOTIVE", length=40.0)
        train = Train(train_id="T1", consist=[car])
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)

        from train_physics import TrainPhysics

        physics = TrainPhysics(train_id=train.train_id, network=network)
        physics._move_train(train, 25.0)

        assert train.position.head_tile == (2, 0)
        assert train.position.head_distance_cm == pytest.approx(25.0)
        assert train.position.tail_tile == (0, 0)
        assert train.position.tail_entry_edge == "W"
        assert train.position.tail_distance_cm == pytest.approx(35.0)


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
        Test that controller transitions from RUNNING_MOVING to RUNNING_WAITING
        when train is stopped AND there's an obstruction (ATC limit = 0).

        The transition to WAITING should ONLY occur when:
        1. Train's current_speed <= threshold (physically stopped)
        2. ATC speed limit <= threshold (obstruction detected)

        Simply being stopped without an obstruction should NOT trigger WAITING.
        """
        network = RailwayNetwork()

        # Create track and block
        track = TrackSegment(x=0, y=0, track_bits=TRACK_EW)
        track.block_section_id = "B1"
        network.add_track(track)

        # Create a block that is occupied by ANOTHER train (obstruction)
        block = network.create_block("B1", "Block 1")
        network.block_manager.occupy_block("B1", "OTHER_TRAIN")

        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_TRANSITION_TEST", consist=[car], max_speed=60.0)
        train.state = TrainState.RUNNING_MOVING
        train.current_speed = 0.0  # Train is stopped
        train.current_pos = (0, 0)
        network.add_train(train)

        sim = SimulationController(network)
        controller = sim._get_or_create_controller(train)
        controller.power_on = True
        controller.state = TrainState.RUNNING_MOVING
        controller.ato_running = True
        controller.active_cab = "A"  # Set cab so ATC calculates speed properly

        # Tick controller - should detect obstruction and transition to WAITING
        sim.update(0.1)

        # Controller should have transitioned to RUNNING_WAITING
        # because there's an obstruction (block occupied by another train)
        assert controller.state == TrainState.RUNNING_WAITING
        assert train.wait_reason is not None

    def test_controller_stays_moving_when_stopped_without_obstruction(self):
        """
        Test that controller stays in RUNNING_MOVING when train is stopped
        but there's NO obstruction (path is clear).

        This verifies that simply reaching speed = 0 doesn't trigger WAITING.
        The train should remain in RUNNING_MOVING if the path ahead is clear.
        """
        network = RailwayNetwork()

        # Create track and block with sufficient length for clearance
        track = TrackSegment(x=0, y=0, track_bits=TRACK_EW)
        track.block_section_id = "B1"
        network.add_track(track)

        block = network.create_block("B1", "Block 1")
        block.length_cm = 1000.0  # 10 meters - sufficient for clearance

        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_NO_OBSTRUCTION", consist=[car], max_speed=60.0)
        train.state = TrainState.RUNNING_MOVING
        train.current_speed = 0.0  # Train is stopped, but path is clear
        train.current_pos = (0, 0)  # Train is on the track
        network.add_train(train)

        sim = SimulationController(network)
        controller = sim._get_or_create_controller(train)
        controller.power_on = True
        controller.state = TrainState.RUNNING_MOVING
        controller.active_cab = "A"  # Set cab so ATC calculates speed properly

        # Tick controller - should NOT transition to WAITING (no obstruction)
        sim.update(0.1)

        # Controller should remain in RUNNING_MOVING (path is clear, no obstruction)
        assert controller.state == TrainState.RUNNING_MOVING

    def test_controller_auto_transitions_waiting_to_moving_when_obstruction_clears(
        self,
    ):
        """
        Test that controller auto-transitions from RUNNING_WAITING back to RUNNING_MOVING
        when the obstruction clears (ATC limit > 0).

        This is the key test for the WAITING ↔ MOVING fix:
        1. Train is in RUNNING_WAITING (blocked by another train)
        2. Obstruction clears (block becomes free)
        3. Train should automatically transition to RUNNING_MOVING
        """
        network = RailwayNetwork()

        # Create track and block
        track = TrackSegment(x=0, y=0, track_bits=TRACK_EW)
        track.block_section_id = "B1"
        network.add_track(track)

        # Create a block that is occupied by ANOTHER train (obstruction)
        # Set block length so ATC clearance calculation works
        block = network.create_block("B1", "Block 1")
        block.length_cm = 1000.0  # 10 meters - sufficient for clearance
        network.block_manager.occupy_block("B1", "OTHER_TRAIN")

        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_WAITING_TEST", consist=[car], max_speed=60.0)
        train.state = TrainState.RUNNING_WAITING
        train.current_speed = 0.0  # Train is stopped
        train.current_pos = (0, 0)
        train.wait_reason = "Block B1 occupied by OTHER_TRAIN"
        network.add_train(train)

        sim = SimulationController(network)
        controller = sim._get_or_create_controller(train)
        controller.power_on = True
        controller.state = TrainState.RUNNING_WAITING
        controller.ato_running = False  # ATO was paused due to waiting
        controller.active_cab = "A"  # Set cab so ATC calculates speed properly

        # Verify initial state
        assert controller.state == TrainState.RUNNING_WAITING

        # Now clear the obstruction - the other train leaves the block
        network.block_manager.set_block_to_caution("B1")
        network.block_manager.clear_block("B1")

        # Tick controller - should detect obstruction cleared and transition to MOVING
        sim.update(0.1)

        # Controller should have transitioned to RUNNING_MOVING
        # because obstruction is gone (block is free)
        assert controller.state == TrainState.RUNNING_MOVING, (
            f"Expected RUNNING_MOVING but got {controller.state}. "
            f"Train should auto-transition when obstruction clears."
        )
        # ATO should be restarted
        assert controller.ato_running == True, (
            "ATO should be restarted after obstruction clears"
        )
        # Wait reason should be cleared
        assert train.wait_reason is None, "Wait reason should be cleared"

    def test_atc_triggers_warning_on_5kmh_overspeed(self):
        """
        Test that ATC triggers warning when overspeed by 5-10 km/h.

        Per new tick() workflow:
        - current_speed > atc_speed + 5 → atc_warning_active = True
        - current_speed > atc_speed + 10 → eb_active = True
        """
        network = RailwayNetwork()

        # Create track and block with speed limit
        track = TrackSegment(x=0, y=0, track_bits=TRACK_EW)
        track.block_section_id = "B1"
        network.add_track(track)

        block = network.create_block("B1", "Block 1")
        block.length_cm = 1000.0
        block.max_speed_kmh = 30.0  # 30 km/h limit

        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_OVERSPEED_TEST", consist=[car], max_speed=100.0)
        train.state = TrainState.RUNNING_MOVING
        train.current_speed = 37.0  # 7 km/h over limit (warning zone)
        train.current_pos = (0, 0)
        network.add_train(train)

        sim = SimulationController(network)
        controller = sim._get_or_create_controller(train)
        controller.power_on = True
        controller.state = TrainState.RUNNING_MOVING
        controller.active_cab = "A"

        # Tick controller
        sim.update(0.1)

        # Should have warning but not EB
        assert controller.atc_warning_active == True, (
            "Warning should be active at 7 km/h overspeed"
        )
        assert controller.eb_active == False, (
            "EB should not trigger at 7 km/h overspeed"
        )

    def test_atc_triggers_eb_on_10kmh_overspeed(self):
        """
        Test that ATC triggers emergency brake when overspeed by > 10 km/h.

        Per new tick() workflow:
        - current_speed > atc_speed + 10 → eb_active = True
        """
        network = RailwayNetwork()

        # Create track and block with speed limit
        track = TrackSegment(x=0, y=0, track_bits=TRACK_EW)
        track.block_section_id = "B1"
        network.add_track(track)

        block = network.create_block("B1", "Block 1")
        block.length_cm = 1000.0
        block.max_speed_kmh = 30.0  # 30 km/h limit

        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_EB_TEST", consist=[car], max_speed=100.0)
        train.state = TrainState.RUNNING_MOVING
        train.current_speed = 42.0  # 12 km/h over limit (EB zone)
        train.current_pos = (0, 0)
        network.add_train(train)

        sim = SimulationController(network)
        controller = sim._get_or_create_controller(train)
        controller.power_on = True
        controller.state = TrainState.RUNNING_MOVING
        controller.active_cab = "A"

        # Tick controller
        sim.update(0.1)

        # Should trigger EB
        assert controller.eb_active == True, "EB should trigger at 12 km/h overspeed"
        assert "overspeed" in controller.speed_limit_reason.lower(), (
            f"Speed limit reason should mention overspeed, got: {controller.speed_limit_reason}"
        )
        # Should have notification
        assert len(controller.pending_notifications) > 0, (
            "Should have notification for EB"
        )

    def test_eb_resets_to_standby_when_stopped(self):
        """
        Test that EB resets to STANDBY state when train stops.

        Per new tick() workflow:
        - When eb_active and train stops → eb_active = False, state = STANDBY
        """
        network = RailwayNetwork()

        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_EB_RESET_TEST", consist=[car], max_speed=60.0)
        train.state = TrainState.RUNNING_MOVING
        train.current_speed = 0.0  # Already stopped
        network.add_train(train)

        sim = SimulationController(network)
        controller = sim._get_or_create_controller(train)
        controller.power_on = True
        controller.eb_active = True  # EB is active
        controller.state = TrainState.RUNNING_MOVING

        # Tick controller - train is stopped, should reset EB
        sim.update(0.1)

        # EB should be released and state should be STANDBY
        assert controller.eb_active == False, "EB should be released when train stopped"
        assert controller.state == TrainState.STANDBY, (
            f"State should be STANDBY after EB reset, got {controller.state}"
        )
        # Should have notification
        assert "released" in " ".join(controller.pending_notifications).lower(), (
            "Should have notification for EB release"
        )

    def test_no_cab_selected_sets_atc_speed_zero(self):
        """
        Test that when no cab is selected, ATC speed is 0.

        Per new tick() workflow:
        - If active_cab is None → atc_speed_limit = 0, reason = "No cab selected"
        """
        network = RailwayNetwork()

        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_NO_CAB_TEST", consist=[car], max_speed=60.0)
        train.state = TrainState.RUNNING_MOVING
        train.current_speed = 0.0
        network.add_train(train)

        sim = SimulationController(network)
        controller = sim._get_or_create_controller(train)
        controller.power_on = True
        controller.state = TrainState.RUNNING_MOVING
        controller.active_cab = None  # No cab selected

        # Tick controller
        sim.update(0.1)

        # ATC speed should be 0
        assert controller.atc_speed_limit == 0.0, (
            "ATC speed should be 0 when no cab selected"
        )
        assert controller.speed_limit_reason == "No cab selected", (
            f"Reason should be 'No cab selected', got: {controller.speed_limit_reason}"
        )

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


class TestTrainPhysicsBlockCautionReleaseBugFix:
    """
    Comprehensive tests for TrainPhysics CAUTION release fix (2026-01-22).

    BUG: TrainPhysics._update_block_occupancy() only transitions OCCUPIED -> CAUTION
    and never clears CAUTION blocks, leaving blocks stuck in CAUTION indefinitely.

    FIX: When a block remains unoccupied after the CAUTION transition, clear it
    to FREE on the next update tick.
    """

    def test_caution_block_clears_after_train_fully_leaves(self):
        """Block should advance from CAUTION to FREE after train fully exits."""
        network = RailwayNetwork()
        block = network.create_block("B1", "Block 1")

        network.block_manager.occupy_block("B1", "TRAIN_1")
        assert block.state == BlockState.OCCUPIED

        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=100.0)
        train = Train(train_id="TRAIN_1", consist=[car], max_speed=60.0)
        network.add_train(train)

        sim = SimulationController(network)
        physics = sim._get_or_create_physics(train)

        physics._last_occupied_blocks = {"B1"}
        train.position = None

        physics._update_block_occupancy(train)
        assert block.state == BlockState.CAUTION

        physics._update_block_occupancy(train)
        assert block.state == BlockState.FREE


class TestTrainPhysicsCautionHoldBugFix:
    """
    Comprehensive tests for TrainPhysics caution hold bug fix (2026-01-23).

    BUG: TrainPhysics._update_block_occupancy() clears CAUTION blocks on the
    next tick even when the train has not vacated another block. This causes
    the block behind the train to become FREE while the train is still moving.

    FIX: Keep the last vacated block in CAUTION until another block is vacated
    or the train leaves the track entirely.
    """

    def test_caution_persists_until_next_block_vacated(self):
        """The last vacated block should remain CAUTION while train stays in place."""
        network = RailwayNetwork()
        track_a = TrackSegment(x=0, y=0, track_bits=TRACK_EW)
        track_b = TrackSegment(x=1, y=0, track_bits=TRACK_EW)
        track_a.block_section_id = "B1"
        track_b.block_section_id = "B2"
        network.add_track(track_a)
        network.add_track(track_b)
        block_a = network.create_block("B1", "Block 1")
        network.create_block("B2", "Block 2")
        network.block_manager.occupy_block("B1", "TRAIN_1")
        assert block_a.state == BlockState.OCCUPIED

        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_1", consist=[car], max_speed=60.0)
        train.current_speed = 10.0
        train.position = TrainPosition(
            end_a_tile=(1, 0),
            end_a_distance_cm=5.0,
            end_a_entry_edge="W",
            end_b_tile=(1, 0),
            end_b_distance_cm=0.0,
            end_b_entry_edge="W",
            leading_end="a",
            facing_direction=Direction.EAST,
            occupied_tiles=[(1, 0)],
        )
        network.add_train(train)

        sim = SimulationController(network)
        physics = sim._get_or_create_physics(train)
        physics._last_occupied_blocks = {"B1"}

        physics._update_block_occupancy(train)
        assert block_a.state == BlockState.CAUTION

        physics._update_block_occupancy(train)
        assert block_a.state == BlockState.CAUTION

    def test_previous_caution_clears_when_next_block_vacated(self):
        """Previous CAUTION block should clear once a new block is vacated."""
        network = RailwayNetwork()
        track_a = TrackSegment(x=0, y=0, track_bits=TRACK_EW)
        track_b = TrackSegment(x=1, y=0, track_bits=TRACK_EW)
        track_c = TrackSegment(x=2, y=0, track_bits=TRACK_EW)
        track_a.block_section_id = "B1"
        track_b.block_section_id = "B2"
        track_c.block_section_id = "B3"
        network.add_track(track_a)
        network.add_track(track_b)
        network.add_track(track_c)

        block_a = network.create_block("B1", "Block 1")
        block_b = network.create_block("B2", "Block 2")
        block_c = network.create_block("B3", "Block 3")
        network.block_manager.occupy_block("B1", "TRAIN_1")

        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_1", consist=[car], max_speed=60.0)
        train.position = TrainPosition(
            end_a_tile=(1, 0),
            end_a_distance_cm=5.0,
            end_a_entry_edge="W",
            end_b_tile=(1, 0),
            end_b_distance_cm=0.0,
            end_b_entry_edge="W",
            leading_end="a",
            facing_direction=Direction.EAST,
            occupied_tiles=[(1, 0)],
        )
        network.add_train(train)

        sim = SimulationController(network)
        physics = sim._get_or_create_physics(train)
        physics._last_occupied_blocks = {"B1"}

        physics._update_block_occupancy(train)
        assert block_a.state == BlockState.CAUTION
        assert block_b.state == BlockState.OCCUPIED

        train.position = TrainPosition(
            end_a_tile=(2, 0),
            end_a_distance_cm=5.0,
            end_a_entry_edge="W",
            end_b_tile=(2, 0),
            end_b_distance_cm=0.0,
            end_b_entry_edge="W",
            leading_end="a",
            facing_direction=Direction.EAST,
            occupied_tiles=[(2, 0)],
        )

        physics._update_block_occupancy(train)
        assert block_a.state == BlockState.FREE
        assert block_b.state == BlockState.CAUTION
        assert block_c.state == BlockState.OCCUPIED


class TestTrainPhysicsTransitions:
    """Tests for TrainPhysics block transition reporting."""

    def test_train_physics_reports_block_transitions(self):
        """TrainPhysics should expose entered and vacated blocks per update."""
        network = RailwayNetwork()
        track_a = TrackSegment(x=0, y=0, track_bits=TRACK_EW)
        track_b = TrackSegment(x=1, y=0, track_bits=TRACK_EW)
        track_a.block_section_id = "B1"
        track_b.block_section_id = "B2"
        network.add_track(track_a)
        network.add_track(track_b)

        network.create_block("B1", "Block 1")
        network.create_block("B2", "Block 2")

        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_1", consist=[car], max_speed=60.0)
        train.position = TrainPosition(
            end_a_tile=(1, 0),
            end_a_distance_cm=5.0,
            end_a_entry_edge="W",
            end_b_tile=(1, 0),
            end_b_distance_cm=0.0,
            end_b_entry_edge="W",
            leading_end="a",
            facing_direction=Direction.EAST,
            occupied_tiles=[(1, 0)],
            occupied_blocks=["B2"],
        )
        network.add_train(train)

        network.block_manager.occupy_block("B1", train.train_id)

        from train_physics import TrainPhysics

        physics = TrainPhysics(train_id=train.train_id, network=network)
        physics._last_occupied_blocks = {"B1"}

        physics._update_block_occupancy(train)

        assert physics.last_block_transitions["entered"] == {"B2"}
        assert physics.last_block_transitions["vacated"] == {"B1"}


class TestTrainPhysicsCautionStoppedReleaseBugFix:
    """
    Comprehensive tests for TrainPhysics caution release on stop (2026-01-23).

    BUG: TrainPhysics keeps the CAUTION block indefinitely when the train stops,
    even though the block behind the occupied block should be clearable once the
    train is stationary.

    FIX: If the train is stopped, clear CAUTION blocks that are no longer occupied.
    """

    def test_caution_clears_when_train_stopped(self):
        """CAUTION block clears when train is stopped and not occupying it."""
        network = RailwayNetwork()
        track_a = TrackSegment(x=0, y=0, track_bits=TRACK_EW)
        track_b = TrackSegment(x=1, y=0, track_bits=TRACK_EW)
        track_a.block_section_id = "B1"
        track_b.block_section_id = "B2"
        network.add_track(track_a)
        network.add_track(track_b)

        block_a = network.create_block("B1", "Block 1")
        block_b = network.create_block("B2", "Block 2")

        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_1", consist=[car], max_speed=60.0)
        train.current_speed = 0.0
        train.position = TrainPosition(
            end_a_tile=(1, 0),
            end_a_distance_cm=5.0,
            end_a_entry_edge="W",
            end_b_tile=(1, 0),
            end_b_distance_cm=0.0,
            end_b_entry_edge="W",
            leading_end="a",
            facing_direction=Direction.EAST,
            occupied_tiles=[(1, 0)],
            occupied_blocks=["B2"],
        )
        network.add_train(train)

        network.block_manager.occupy_block("B1", train.train_id)
        network.block_manager.set_block_to_caution("B1")
        network.block_manager.occupy_block("B2", train.train_id)

        sim = SimulationController(network)
        physics = sim._get_or_create_physics(train)
        physics._last_occupied_blocks = {"B2"}
        physics._caution_blocks = {"B1"}

        physics._update_block_occupancy(train)

        assert block_a.state == BlockState.FREE
        assert block_b.state == BlockState.OCCUPIED


class TestTrainControlPanelDataBugFix:
    """
    Comprehensive tests for train control panel data propagation fix (2026-01-22).

    BUG: /api/network returned train data without controller state, so the
    train control panel could not reflect power/handle status or ATO path info.

    FIX: /api/network includes an onboard controller snapshot per train.
    """

    def test_network_includes_onboard_snapshot(self):
        """Network response should include onboard controller state per train."""
        from simulation import build_network_payload

        network = RailwayNetwork()
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_01", consist=[car], max_speed=60.0)
        train.assigned_path_id = "PATH_1"
        network.add_train(train)

        sim_controller = SimulationController(network)
        controller = sim_controller._get_or_create_controller(train)
        controller.power_on = True
        controller.handle_position = HandlePosition.P2
        controller.control_mode = "ato"

        payload = build_network_payload(network, sim_controller)
        train_data = payload["trains"][0]
        assert "onboard" in train_data
        assert train_data["onboard"]["power_on"] is True
        assert train_data["onboard"]["handle_position"] == "P2"
        assert train_data["onboard"]["control_mode"] == "ato"
        assert "ato_info" in train_data["onboard"]


class TestEmergencyResetButtonDataBugFix:
    """
    Comprehensive tests for emergency reset button data fix (2026-01-24).

    BUG: Onboard controller payload only included `eb_active`, while the
    control panel expects `emergency_stop_active`, so the reset button never
    appears even when emergency braking is active.

    FIX: Provide `emergency_stop_active` in onboard snapshots alongside
    `eb_active`.
    """

    def test_onboard_includes_emergency_stop_active(self):
        """Onboard payload should expose emergency_stop_active for UI state."""
        from simulation import build_network_payload

        network = RailwayNetwork()
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_01", consist=[car], max_speed=60.0)
        network.add_train(train)

        sim_controller = SimulationController(network)
        controller = sim_controller._get_or_create_controller(train)
        controller.eb_active = True

        payload = build_network_payload(network, sim_controller)
        train_data = payload["trains"][0]
        assert train_data["onboard"]["emergency_stop_active"] is True


class TestTrainDebugLogging:
    """Regression tests for controller/physics debug logging (2026-01-22)."""

    def test_train_controller_logs_power_off_state(self, caplog):
        """TrainController.tick should log when power is off."""
        network = RailwayNetwork()
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_LOG_1", consist=[car], max_speed=60.0)
        network.add_train(train)

        sim = SimulationController(network)
        controller = sim._get_or_create_controller(train)
        controller.power_on = False

        caplog.set_level(logging.DEBUG)
        controller.tick(0.1)

        assert any(
            "power off" in record.message.lower() for record in caplog.records
        ), "Expected debug log for power off state"


class TestTrainClearancePrecisionBugFix:
    """
    Comprehensive tests for clearance precision bug fix (2026-01-22).

    BUG: clearance uses legacy current_pos/distance_on_track and ignores TrainPosition,
    causing clearance to jump when the head enters the last tile.

    FIX: clearance derives remaining distance from TrainPosition head location.
    """

    def test_remaining_distance_uses_train_position_head(self):
        """Remaining distance should reflect head distance in current tile."""
        network = RailwayNetwork()

        track_a = TrackSegment(x=0, y=0, track_bits=TRACK_EW)
        track_b = TrackSegment(x=1, y=0, track_bits=TRACK_EW)
        track_a.block_section_id = "B1"
        track_b.block_section_id = "B1"
        network.add_track(track_a)
        network.add_track(track_b)

        block = network.create_block("B1", "Block 1")
        block.length_cm = 40.0

        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=10.0)
        train = Train(train_id="TRAIN_CLEARANCE", consist=[car], max_speed=60.0)
        train.current_pos = (0, 0)
        train.current_path = [(0, 0, Direction.EAST), (1, 0, Direction.EAST)]

        train.position = TrainPosition(
            end_a_tile=(1, 0),
            end_a_distance_cm=5.0,
            end_a_entry_edge="W",
            end_b_tile=(0, 0),
            end_b_distance_cm=5.0,
            end_b_entry_edge="W",
            leading_end="a",
            facing_direction=Direction.EAST,
            occupied_tiles=[(1, 0), (0, 0)],
        )

        network.add_train(train)
        controller = TrainController(train_id=train.train_id, network=network)

        remaining = controller._get_remaining_distance_in_current_block()

        assert remaining == pytest.approx(15.0), (
            f"Expected 15.0 cm remaining, got {remaining}"
        )


class TestTrainPhysicsDebugLogging:
    """Regression tests for TrainPhysics debug details (2026-01-22)."""

    def test_train_physics_tick_updates_speed_correctly(self):
        """TrainPhysics.tick should update speed based on handle position."""
        network = RailwayNetwork()
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_PHYS_LOG", consist=[car], max_speed=60.0)
        train.current_speed = 12.0
        network.add_train(train)

        sim = SimulationController(network)
        controller = sim._get_or_create_controller(train)
        physics = sim._get_or_create_physics(train)

        controller.power_on = True
        controller.handle_position = HandlePosition.P2
        controller.target_speed = 40.0

        initial_speed = train.current_speed
        physics.tick(0.1)

        # Speed should increase with P2 handle (power)
        assert train.current_speed > initial_speed


class TestTrainAssignedPathSerializationBugFix:
    """
    Comprehensive tests for assigned_path_id serialization fix (2026-01-22).

    BUG: Train.to_dict omitted assigned_path_id, causing frontend to show "No path"
    after UI refresh or cab switch.

    FIX: Train.to_dict includes assigned_path_id for frontend state.
    """

    def test_train_to_dict_includes_assigned_path_id(self):
        """Train serialization should include assigned_path_id."""
        network = RailwayNetwork()
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_PATH", consist=[car], max_speed=60.0)
        train.assigned_path_id = "PATH_123"
        network.add_train(train)

        data = train.to_dict(network)

        assert data.get("assigned_path_id") == "PATH_123"

    def test_train_physics_power_off_does_not_update_speed(self):
        """TrainPhysics.tick should not update speed when power is off."""
        network = RailwayNetwork()
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_LOG_2", consist=[car], max_speed=60.0)
        train.current_speed = 10.0
        network.add_train(train)

        sim = SimulationController(network)
        controller = sim._get_or_create_controller(train)
        physics = sim._get_or_create_physics(train)
        controller.power_on = False

        initial_speed = train.current_speed
        physics.tick(0.1)

        # Speed should remain unchanged when power is off
        assert train.current_speed == initial_speed

    def test_target_speed_does_not_override_power_handle(self):
        """
        Test that target_speed does not force deceleration in TrainPhysics.

        BUG: train_physics.py:204-221 forces decel and clamps speed when
        target_speed < current_speed, even with power handle.

        FIX: Speed changes should be driven by handle position; target_speed
        is enforced by TrainController, not TrainPhysics.
        """
        network = RailwayNetwork()
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=100.0)
        train = Train(train_id="TRAIN_FRICTION_TEST", consist=[car], max_speed=60.0)
        train.current_speed = 10.0  # Train is moving
        network.add_train(train)

        sim = SimulationController(network)
        controller = sim._get_or_create_controller(train)
        physics = sim._get_or_create_physics(train)

        # Set controller state: power on, but target speed is 0 (should not stop)
        controller.power_on = True
        controller.handle_position = HandlePosition.P1  # Power handle
        controller.target_speed = 0.0  # But target is to stop

        initial_speed = train.current_speed

        # Tick physics - train should accelerate due to power handle
        physics.tick(0.1)

        # Train should have sped up (handle-driven), not slowed down
        assert train.current_speed > initial_speed, (
            f"Train should accelerate with power handle even if target_speed=0, "
            f"but speed went from {initial_speed} to {train.current_speed}"
        )


class TestTrainControllerBrakingNotches:
    """Tests for braking notch selection updates (2026-01-22)."""

    def test_braking_notch_for_speed_uses_b1_to_b8(self):
        """Speed-based braking should map to B1-B8 notches."""
        network = RailwayNetwork()
        train = Train(train_id="TRAIN_BRAKE", max_speed=60.0)
        network.add_train(train)

        controller = TrainController(train_id=train.train_id, network=network)

        assert (
            controller._calculate_braking_notch_for_speed(60.0, 25.0)
            == HandlePosition.B8
        )
        assert (
            controller._calculate_braking_notch_for_speed(40.0, 14.0)
            == HandlePosition.B7
        )
        assert (
            controller._calculate_braking_notch_for_speed(30.0, 8.0)
            == HandlePosition.B6
        )
        assert (
            controller._calculate_braking_notch_for_speed(22.0, 7.0)
            == HandlePosition.B5
        )
        assert (
            controller._calculate_braking_notch_for_speed(18.0, 7.0)
            == HandlePosition.B4
        )
        assert (
            controller._calculate_braking_notch_for_speed(12.0, 6.0)
            == HandlePosition.B3
        )
        assert (
            controller._calculate_braking_notch_for_speed(9.0, 6.0) == HandlePosition.B2
        )
        assert (
            controller._calculate_braking_notch_for_speed(7.0, 6.0) == HandlePosition.B1
        )

    def test_stop_braking_notch_uses_b1_to_b8(self):
        """Stop braking should map to B1-B8 based on speed."""
        network = RailwayNetwork()
        train = Train(train_id="TRAIN_STOP", max_speed=60.0)
        network.add_train(train)

        controller = TrainController(train_id=train.train_id, network=network)

        assert controller._calculate_braking_notch(35.0) == HandlePosition.B8
        assert controller._calculate_braking_notch(27.0) == HandlePosition.B7
        assert controller._calculate_braking_notch(22.0) == HandlePosition.B6
        assert controller._calculate_braking_notch(16.0) == HandlePosition.B5
        assert controller._calculate_braking_notch(12.0) == HandlePosition.B4
        assert controller._calculate_braking_notch(8.0) == HandlePosition.B3
        assert controller._calculate_braking_notch(4.0) == HandlePosition.B2
        assert controller._calculate_braking_notch(1.0) == HandlePosition.B1

    def test_manual_mode_uses_atc_speed_braking(self, monkeypatch):
        """Manual mode should brake toward ATC speed, not full stop."""
        network = RailwayNetwork()
        train = Train(train_id="TRAIN_MANUAL", max_speed=60.0)
        train.current_speed = 16.0
        network.add_train(train)

        controller = TrainController(train_id=train.train_id, network=network)
        controller.control_mode = "manual"
        controller.power_on = True
        controller.active_cab = "A"

        monkeypatch.setattr(controller, "_calculate_clearance_ahead", lambda: 200.0)
        monkeypatch.setattr(
            controller,
            "_calculate_atc_speed_limit",
            lambda: (10.0, "test"),
        )

        controller.tick(0.1)

        assert controller.handle_position == HandlePosition.B3

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
