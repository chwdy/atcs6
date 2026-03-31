"""
Tests for live path data flow: TrainController -> to_dict() -> frontend.
"""

import pytest
from network import RailwayNetwork
from track import TrackSegment, TRACK_EW
from enums import Direction
from train import Train, TrainCar
from path_dispatcher import PathDispatcher
from train_controller import create_controller_for_train


@pytest.fixture
def live_path_network():
    """Network with 3 blocks of 3 tiles each for path testing."""
    network = RailwayNetwork()

    for x in range(9):
        track = TrackSegment(x=x, y=0, track_bits=TRACK_EW)
        network.add_track(track)

    network.create_block_with_tracks(
        "BLK_A", [(x, 0) for x in range(3)], name="Block A"
    )
    network.create_block_with_tracks(
        "BLK_B", [(x, 0) for x in range(3, 6)], name="Block B"
    )
    network.create_block_with_tracks(
        "BLK_C", [(x, 0) for x in range(6, 9)], name="Block C"
    )

    return network


class TestGetLivePathData:
    """Tests for TrainController.get_live_path_data()."""

    def test_returns_none_when_no_path(self, live_path_network):
        """Returns None when train has no assigned path."""
        network = live_path_network
        train = Train(
            train_id="T1",
            consist=[TrainCar(car_id="C1", car_type="LOCOMOTIVE", length=40.0)],
        )
        network.add_train(train)

        dispatcher = PathDispatcher(network)
        controller = create_controller_for_train(
            train_id=train.train_id,
            network=network,
            path_dispatcher=dispatcher,
        )

        result = controller.get_live_path_data()
        assert result is None

    def test_returns_path_tiles_and_index(self, live_path_network):
        """Returns dict with path_tiles and path_index when path exists."""
        network = live_path_network
        train = Train(
            train_id="T1",
            consist=[TrainCar(car_id="C1", car_type="LOCOMOTIVE", length=40.0)],
        )
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)

        dispatcher = PathDispatcher(network)
        result = dispatcher.create_path(
            start_block_id="BLK_A",
            end_block_id="BLK_B",
            train_id=train.train_id,
        )
        assert result.success
        train.assigned_path_id = result.path.path_id

        controller = create_controller_for_train(
            train_id=train.train_id,
            network=network,
            path_dispatcher=dispatcher,
        )

        live_data = controller.get_live_path_data()
        assert live_data is not None
        assert "path_tiles" in live_data
        assert "path_index" in live_data
        assert len(live_data["path_tiles"]) > 0
        assert live_data["path_index"] >= 0

    def test_reflects_extension_immediately(self, live_path_network):
        """After extend_path, get_live_path_data returns extended path."""
        network = live_path_network
        train = Train(
            train_id="T1",
            consist=[TrainCar(car_id="C1", car_type="LOCOMOTIVE", length=40.0)],
        )
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)

        dispatcher = PathDispatcher(network)
        result = dispatcher.create_path(
            start_block_id="BLK_A",
            end_block_id="BLK_B",
            train_id=train.train_id,
        )
        assert result.success
        train.assigned_path_id = result.path.path_id

        controller = create_controller_for_train(
            train_id=train.train_id,
            network=network,
            path_dispatcher=dispatcher,
        )

        before = controller.get_live_path_data()
        count_before = len(before["path_tiles"])

        ext = dispatcher.extend_path(result.path.path_id, "BLK_C")
        assert ext.success

        after = controller.get_live_path_data()
        assert len(after["path_tiles"]) > count_before

    def test_path_tiles_have_direction(self, live_path_network):
        """Each path tile entry should be (x, y, Direction)."""
        network = live_path_network
        train = Train(
            train_id="T1",
            consist=[TrainCar(car_id="C1", car_type="LOCOMOTIVE", length=40.0)],
        )
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)

        dispatcher = PathDispatcher(network)
        result = dispatcher.create_path(
            start_block_id="BLK_A",
            end_block_id="BLK_B",
            train_id=train.train_id,
        )
        assert result.success
        train.assigned_path_id = result.path.path_id

        controller = create_controller_for_train(
            train_id=train.train_id,
            network=network,
            path_dispatcher=dispatcher,
        )

        live_data = controller.get_live_path_data()
        for entry in live_data["path_tiles"]:
            assert len(entry) == 3
            assert isinstance(entry[2], Direction)
