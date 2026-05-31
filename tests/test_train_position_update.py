"""
Placeholder for train position end_a/end_b updates during simulation.

v1: live position lives on `TrainController.physics_sync.current_span`
(`TrainSpan`), not on `Train`. This module's fixture is kept as a stub; the
end-point-update behavior is covered by the demo journey and the movement
tests in tests/test_trains_physics.py.
"""

import pytest
from train import Train, TrainCar
from network import RailwayNetwork
from track import TrackSegment, TRACK_EW
from enums import Direction, TrainState
from simulation import SimulationController


class TestTrainPositionUpdates:
    """Test that train position end points update during simulation movement."""

    @pytest.fixture
    def simple_network(self):
        """Create simple straight E-W track for testing."""
        network = RailwayNetwork()
        for x in range(5):
            track = TrackSegment(x=x, y=0, track_bits=TRACK_EW)
            network.add_track(track)
        return network

    @pytest.fixture
    def demo_train(self, simple_network):
        """Create a train at (0, 0) facing EAST.

        v1: the live span is seeded by `SimulationController.add_train(...)` /
        `network.place_train(...)`; this fixture builds the Train + network for
        any test that wants to drive movement. State / current_speed / path
        geometry live on TrainController / PathDispatcher.
        """
        train = Train(
            train_id="DEMO_TRAIN",
            consist=[TrainCar(car_id="loco1", car_type="LOCOMOTIVE", length_cm=40.0)],
            max_speed=80.0,
        )
        simple_network.add_train(train)
        simple_network.place_train(train, 0, 0, Direction.EAST)
        return train


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
