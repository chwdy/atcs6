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


class TestToDictLivePath:
    """Tests that to_dict() uses live path data when provided."""

    def test_to_dict_uses_provided_path_tiles(self, live_path_network):
        """to_dict() should use path_tiles param over self.current_path."""
        network = live_path_network
        train = Train(
            train_id="T1",
            consist=[TrainCar(car_id="C1", car_type="LOCOMOTIVE", length=40.0)],
        )
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)

        # Set stale data on train
        train.current_path = [(0, 0, Direction.EAST), (1, 0, Direction.EAST)]
        train.path_index = 0

        # Provide "live" data with more tiles
        live_tiles = [
            (0, 0, Direction.EAST),
            (1, 0, Direction.EAST),
            (2, 0, Direction.EAST),
            (3, 0, Direction.EAST),
            (4, 0, Direction.EAST),
        ]

        data = train.to_dict(network=network, path_tiles=live_tiles, path_index=2)

        assert data["path_length"] == 5, "Should use live path length, not stale"
        assert data["path_index"] == 2, "Should use live path_index, not stale"
        assert len(data["current_path"]) == 5

    def test_to_dict_falls_back_to_stale_when_no_params(self, live_path_network):
        """to_dict() falls back to self.current_path when no params provided."""
        network = live_path_network
        train = Train(
            train_id="T1",
            consist=[TrainCar(car_id="C1", car_type="LOCOMOTIVE", length=40.0)],
        )
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)

        train.current_path = [(0, 0, Direction.EAST), (1, 0, Direction.EAST)]
        train.path_index = 1

        data = train.to_dict(network=network)

        assert data["path_length"] == 2
        assert data["path_index"] == 1

    def test_to_dict_lookahead_uses_live_path(self, live_path_network):
        """Lookahead tile info should be built from live path, not stale."""
        network = live_path_network
        train = Train(
            train_id="T1",
            consist=[TrainCar(car_id="C1", car_type="LOCOMOTIVE", length=40.0)],
        )
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)

        # Stale: 2 tiles
        train.current_path = [(0, 0, Direction.EAST), (1, 0, Direction.EAST)]
        train.path_index = 0

        # Live: 6 tiles
        live_tiles = [
            (0, 0, Direction.EAST),
            (1, 0, Direction.EAST),
            (2, 0, Direction.EAST),
            (3, 0, Direction.EAST),
            (4, 0, Direction.EAST),
            (5, 0, Direction.EAST),
        ]

        data = train.to_dict(network=network, path_tiles=live_tiles, path_index=0)

        # Lookahead should cover all 6 tiles, not just 2
        assert "lookahead_tile_info" in data
        assert len(data["lookahead_tile_info"]) == 6
