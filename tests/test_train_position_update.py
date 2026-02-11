"""
Test to verify train position end_a/end_b updates during simulation.

This test investigates the backend issue where train.position.end_a and
train.position.end_b remain stuck at initialization values during movement,
even though train.position.distance IS updating correctly.
"""

import pytest
from train import Train, TrainCar, TrainPosition
from network import RailwayNetwork
from track import TrackSegment, TRACK_EW
from enums import Direction, LeadingEnd, TrainState
from simulation import SimulationController


class TestTrainPositionUpdates:
    """Test that train position end points update during simulation movement."""

    @pytest.fixture
    def simple_network(self):
        """Create simple straight E-W track for testing."""
        network = RailwayNetwork()

        # Create 5 tiles of straight E-W track
        for x in range(5):
            track = TrackSegment(x=x, y=0, track_bits=TRACK_EW)
            network.add_track(track)

        return network

    @pytest.fixture
    def demo_train(self, simple_network):
        """Create a train positioned at (0, 0) facing EAST."""
        train = Train(
            train_id="DEMO_TRAIN",
            consist=[TrainCar(car_id="loco1", car_type="LOCOMOTIVE", length=40.0)],
            current_speed=0.0,
            max_speed=80.0,
        )

        # Create TrainPosition with initial end_a and end_b
        train.position = TrainPosition(
            end_a_tile=(0, 0),
            end_a_distance_cm=50.0,  # Head at 50cm
            end_a_entry_edge="W",
            end_b_tile=(0, 0),
            end_b_distance_cm=10.0,  # Tail at 10cm (40cm train length)
            end_b_entry_edge="W",
            leading_end=LeadingEnd.A,
            facing_direction=Direction.EAST,
        )

        # Set state to RUNNING_MOVING
        train.state = TrainState.RUNNING_MOVING
        train.target_speed = 50.0
        train.current_speed = 50.0

        # Create simple path (straight line)
        train.current_path = [
            (0, 0, Direction.EAST),
            (1, 0, Direction.EAST),
            (2, 0, Direction.EAST),
            (3, 0, Direction.EAST),
            (4, 0, Direction.EAST),
        ]
        train.path_index = 0

        simple_network.add_train(train)
        return train

    def test_position_end_a_updates_during_movement(self, simple_network, demo_train):
        """
        CRITICAL TEST: Verify that end_a values change during simulation.

        This test checks if train.position.end_a.distance_cm updates
        as the train moves through the network.
        """
        sim = SimulationController(simple_network)

        # Get initial state
        initial_distance = demo_train.position.end_a_distance_cm
        initial_tile = demo_train.position.end_a_tile

        print(f"\n[BEFORE SIMULATION]")
        print(f"  end_a.tile: {demo_train.position.end_a_tile}")
        print(f"  end_a.distance_cm: {demo_train.position.end_a_distance_cm}")
        print(f"  end_a.entry_edge: {demo_train.position.end_a_entry_edge}")
        print(f"  end_b.tile: {demo_train.position.end_b_tile}")
        print(f"  end_b.distance_cm: {demo_train.position.end_b_distance_cm}")

        # Run simulation for 1 second (should move the train)
        sim.update(1.0)

        print(f"\n[AFTER SIMULATION (1 sec)]")
        print(f"  end_a.tile: {demo_train.position.end_a_tile}")
        print(f"  end_a.distance_cm: {demo_train.position.end_a_distance_cm}")
        print(f"  end_a.entry_edge: {demo_train.position.end_a_entry_edge}")
        print(f"  end_b.tile: {demo_train.position.end_b_tile}")
        print(f"  end_b.distance_cm: {demo_train.position.end_b_distance_cm}")

        # Serialize to dict (what frontend sees)
        train_dict = demo_train.to_dict(simple_network)

        print(f"\n[SERIALIZED TO FRONTEND]")
        print(f"  position.end_a: {train_dict.get('position', {}).get('end_a')}")
        print(f"  position.end_b: {train_dict.get('position', {}).get('end_b')}")
        # ASSERTIONS

        # CRITICAL: end_a.distance_cm should have changed
        # This is the BUG - it likely won't change
        print(f"\n[CHECKING IF end_a.distance_cm CHANGED]")
        print(f"  Initial: {initial_distance}")
        print(f"  Current: {demo_train.position.end_a_distance_cm}")
        print(f"  Changed: {demo_train.position.end_a_distance_cm != initial_distance}")

        # This assertion will likely FAIL - that's the bug
        assert demo_train.position.end_a_distance_cm != initial_distance, (
            "BUG REPRODUCED: end_a.distance_cm should change during movement"
        )

    def test_to_dict_serialization(self, simple_network, demo_train):
        """
        Test what data is actually sent to frontend via to_dict().

        This checks if the serialization logic properly includes
        updated end_a/end_b values.
        """
        # Run simulation
        sim = SimulationController(simple_network)
        sim.update(0.5)

        # Get serialized data (what API returns)
        train_dict = demo_train.to_dict(simple_network)

        print(f"\n[SERIALIZED TRAIN DATA]")
        print(f"  Train ID: {train_dict['id']}")
        print(f"  Speed: {train_dict['speed']}")
        print(f"  Position dict keys: {train_dict.get('position', {}).keys()}")

        position = train_dict.get("position", {})
        if "end_a" in position:
            print(f"  end_a: {position['end_a']}")
            print(f"  end_b: {position['end_b']}")
        else:
            print(f"  ❌ end_a/end_b NOT in serialized position")

        # Check if end_a and end_b are present
        assert "position" in train_dict, "Position should be in serialized data"
        assert "end_a" in position, "end_a should be in position data"
        assert "end_b" in position, "end_b should be in position data"

        # Check structure
        assert "tile" in position["end_a"], "end_a should have tile"
        assert "distance_cm" in position["end_a"], "end_a should have distance_cm"
        assert "entry_edge" in position["end_a"], "end_a should have entry_edge"

    def test_occupied_tiles_order_leading_end_a(self, simple_network, demo_train):
        """
        Test that occupied_tiles is ordered from end_a to end_b when leading_end='a'.

        This verifies the fix for the issue where _calculate_occupied_tiles returns
        tiles from tail to head, but occupied_tiles should be ordered from end_a to end_b.
        """
        # Run simulation to move train
        sim = SimulationController(simple_network)
        sim.update(1.0)

        # Get serialized data
        train_dict = demo_train.to_dict(simple_network)
        position = train_dict.get("position", {})

        # Verify leading_end is 'a' (end_a is head)
        assert position.get("leading_end") == "a", "leading_end should be 'a'"

        # Get occupied_tiles from position
        occupied_tiles = position.get("occupied_tiles", [])

        # First tile should be end_a (head), last tile should be end_b (tail)
        end_a_tile = tuple(position["end_a"]["tile"])
        end_b_tile = tuple(position["end_b"]["tile"])

        first_tile = tuple(occupied_tiles[0])
        last_tile = tuple(occupied_tiles[-1])

        print(f"\n[OCCUPIED TILES ORDER TEST]")
        print(f"  end_a (head): {end_a_tile}")
        print(f"  end_b (tail): {end_b_tile}")
        print(f"  First tile in list: {first_tile}")
        print(f"  Last tile in list: {last_tile}")

        assert first_tile == end_a_tile, (
            f"First tile {first_tile} should be end_a (head) {end_a_tile}"
        )
        assert last_tile == end_b_tile, (
            f"Last tile {last_tile} should be end_b (tail) {end_b_tile}"
        )

        # Verify path_tiles represents lookahead (from head to end of path)
        # NOT the same as occupied_tiles (which is just tiles the train is on)
        path_tiles = train_dict.get("path_tiles", [])

        # path_tiles[0] should be the current head tile (matches end_a)
        assert path_tiles[0] == list(end_a_tile), (
            "path_tiles[0] should be end_a (head) tile"
        )

        # path_tiles should extend to end of path (not end_b)
        # It's the lookahead for TrainMonitor, showing tiles ahead
        assert len(path_tiles) >= len(occupied_tiles), (
            "path_tiles (lookahead) should be >= occupied_tiles"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
