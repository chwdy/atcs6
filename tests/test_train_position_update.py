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
            consist=[TrainCar(car_id="loco1", car_type="LOCOMOTIVE", length_cm=40.0)],
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
        )

        simple_network.add_train(train)
        # State, current_speed, and path geometry live on TrainController /
        # PathDispatcher in v1; tests reviving this fixture should drive
        # those owners directly rather than writing onto Train.
        return train





if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
