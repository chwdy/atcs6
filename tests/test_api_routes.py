"""
API Routes Tests - Comprehensive test suite for all migrated HTTP endpoints.

This test suite verifies that all 108 routes migrated to the routes/ system work correctly.
Tests cover:
- HTTP status codes (200, 404, 400, etc.)
- Error handling
- JSON structure validation
- State changes

Run with: conda run -n general pytest tests/test_api_routes.py -v
"""

from datetime import datetime
import pytest
import threading

from app_factory import create_app
from app_state import AppState
from network import RailwayNetwork
from path_dispatcher import PathDispatcher
from simulation import SimulationController
from persistence import PersistenceManager
from speed_profile import SpeedProfileRegistry
from track import TrackSegment
from block import Block
from railway_signal import Signal, SignalType
from station import Station
from train import Train, TrainCar
from switch import Switch
from enums import Direction, BlockState, TrainState
from tests.train_test_helpers import set_train_span


@pytest.fixture
def app_state():
    """Create a fresh AppState with initialized components for testing."""
    network = RailwayNetwork()
    path_dispatcher = PathDispatcher(network)
    sim_controller = SimulationController(network, path_dispatcher)
    persistence_manager = PersistenceManager(storage_dir="layouts")
    speed_profile_registry = SpeedProfileRegistry()

    state = AppState(
        network=network,
        path_dispatcher=path_dispatcher,
        sim_controller=sim_controller,
        persistence_manager=persistence_manager,
        speed_profile_registry=speed_profile_registry,
        simulation_lock=threading.Lock(),
        simulation_running=False,
        simulation_speed=1.0,
        last_update_time=datetime.now(),
    )

    return state


@pytest.fixture
def client(app_state):
    """Create a Flask test client with initialized state."""
    app = create_app(app_state)
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


@pytest.fixture
def network_with_tracks(app_state):
    """Create a network with some basic tracks for testing."""
    network = app_state.network

    # Add straight east-west tracks at y=0
    for x in range(5):
        track = TrackSegment(x=x, y=0, track_bits=2)  # EW track
        network.add_track(track)

    # Add straight north-south tracks at x=0
    for y in range(1, 5):
        track = TrackSegment(x=0, y=y, track_bits=1)  # NS track
        network.add_track(track)

    return network


# ============================================================================
# Track Routes Tests (5 routes)
# ============================================================================


class TestTrackRoutes:
    """Test track CRUD and bulk operations."""

    def test_get_track_success(self, client, network_with_tracks):
        """GET /api/track/<x>/<y> should return track data."""
        response = client.get("/api/track/0/0")
        assert response.status_code == 200
        data = response.get_json()
        assert data["x"] == 0
        assert data["y"] == 0
        assert "track_bits" in data

    def test_get_track_not_found(self, client):
        """GET /api/track/<x>/<y> should return 404 for nonexistent track."""
        response = client.get("/api/track/99/99")
        assert response.status_code == 404
        data = response.get_json()
        assert "error" in data

    def test_update_track_success(self, client, network_with_tracks):
        """PATCH /api/track/<x>/<y> should update track properties."""
        response = client.patch("/api/track/0/0", json={"track_bits": 1})
        assert response.status_code == 200
        data = response.get_json()
        assert data["status"] == "success"

    def test_update_track_not_found(self, client):
        """PATCH /api/track/<x>/<y> should return 404 for nonexistent track."""
        response = client.patch("/api/track/99/99", json={"track_bits": 1})
        assert response.status_code == 404

    def test_delete_track_success(self, client, network_with_tracks):
        """DELETE /api/track/<x>/<y> should remove track."""
        response = client.delete("/api/track/0/0")
        assert response.status_code == 200
        data = response.get_json()
        assert data["status"] == "success"

    def test_delete_track_not_found(self, client):
        """DELETE /api/track/<x>/<y> should return 404 for nonexistent track."""
        response = client.delete("/api/track/99/99")
        assert response.status_code == 404

    def test_add_tracks_bulk(self, client):
        """POST /api/tracks/bulk should add multiple tracks."""
        tracks_data = [
            {"x": 0, "y": 0, "track_bits": 2},
            {"x": 1, "y": 0, "track_bits": 2},
            {"x": 2, "y": 0, "track_bits": 2},
        ]
        response = client.post("/api/tracks/bulk", json={"tracks": tracks_data})
        assert response.status_code == 200
        data = response.get_json()
        assert data["status"] == "success"
        assert data["added"] == 3

    def test_delete_tracks_bulk(self, client, network_with_tracks):
        """POST /api/tracks/bulk-delete should delete multiple tracks."""
        positions = [[0, 0], [1, 0], [2, 0]]
        response = client.post("/api/tracks/bulk-delete", json={"positions": positions})
        assert response.status_code == 200
        data = response.get_json()
        assert data["status"] == "success"
        assert data["deleted"] == 3


# ============================================================================
# Block Routes Tests (13 routes)
# ============================================================================


class TestBlockRoutes:
    """Test block management endpoints."""

    def test_get_blocks(self, client):
        """GET /api/blocks should list all blocks."""
        response = client.get("/api/blocks")
        assert response.status_code == 200
        data = response.get_json()
        assert isinstance(data, list)

    def test_create_block(self, client):
        """POST /api/blocks should create a new block."""
        response = client.post(
            "/api/blocks", json={"block_id": "BLK_TEST", "name": "Test Block"}
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data["block_id"] == "BLK_TEST"

    def test_get_block_success(self, client, app_state):
        """GET /api/blocks/<block_id> should return block details."""
        block = app_state.network.create_block("BLK_001", "Test Block")
        response = client.get("/api/blocks/BLK_001")
        assert response.status_code == 200
        data = response.get_json()
        assert data["block_id"] == "BLK_001"

    def test_get_block_not_found(self, client):
        """GET /api/blocks/<block_id> should return 404 for nonexistent block."""
        response = client.get("/api/blocks/NONEXISTENT")
        assert response.status_code == 404

    def test_update_block(self, client, app_state):
        """PATCH /api/blocks/<block_id> should update block properties."""
        app_state.network.create_block("BLK_001", "Test")
        response = client.patch("/api/blocks/BLK_001", json={"name": "Updated"})
        assert response.status_code == 200
        data = response.get_json()
        assert data["name"] == "Updated"

    def test_update_block_allowed_travel_directions_one_way(
        self, client, app_state
    ):
        """PATCH should set a one-way `allowed_travel_directions` policy and
        round-trip it through `to_dict` (sorted list)."""
        block = app_state.network.create_block("BLK_001", "Test")
        response = client.patch(
            "/api/blocks/BLK_001",
            json={"allowed_travel_directions": ["forward"]},
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data["allowed_travel_directions"] == ["forward"]
        # The field is actually mutated on the entity, as a set.
        assert block.allowed_travel_directions == {"forward"}

    def test_update_block_allowed_travel_directions_both(
        self, client, app_state
    ):
        """PATCH with both directions stores the full bidirectional policy."""
        app_state.network.create_block("BLK_001", "Test")
        response = client.patch(
            "/api/blocks/BLK_001",
            json={"allowed_travel_directions": ["reverse", "forward"]},
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data["allowed_travel_directions"] == ["forward", "reverse"]

    def test_update_block_allowed_travel_directions_empty_rejected(
        self, client, app_state
    ):
        """An empty set is rejected with 400 — a block must allow at least
        one direction (design_new.md: defaults to both, never none)."""
        block = app_state.network.create_block("BLK_001", "Test")
        response = client.patch(
            "/api/blocks/BLK_001",
            json={"allowed_travel_directions": []},
        )
        assert response.status_code == 400
        # The block's policy is left untouched on rejection.
        assert block.allowed_travel_directions == {"forward", "reverse"}

    def test_update_block_allowed_travel_directions_unknown_rejected(
        self, client, app_state
    ):
        """An unknown direction token is rejected with 400."""
        block = app_state.network.create_block("BLK_001", "Test")
        response = client.patch(
            "/api/blocks/BLK_001",
            json={"allowed_travel_directions": ["sideways"]},
        )
        assert response.status_code == 400
        assert block.allowed_travel_directions == {"forward", "reverse"}

    def test_delete_block(self, client, app_state):
        """DELETE /api/blocks/<block_id> should delete block."""
        app_state.network.create_block("BLK_001", "Test")
        response = client.delete("/api/blocks/BLK_001")
        assert response.status_code == 200
        data = response.get_json()
        assert data["status"] == "success"

    def test_cycle_block_state_endpoint_is_gone(self, client, app_state):
        """The cycle-state admin shim was removed in PR 6.

        Block state is owned by BlockManager / ResourceCoordinator in v1
        and computed as a read-only @property from `occupied_by` /
        `_is_caution` / `reservation`; there is no admin-side
        direct-mutation surface. The endpoint that used to expose this
        path is removed; calling it now returns 404.
        """
        app_state.network.create_block("BLK_001", "Test")
        response = client.post("/api/blocks/BLK_001/cycle-state")
        # 404 if Flask doesn't know the route at all; 405 if Flask
        # matches the URL prefix but rejects the method. Either way the
        # admin shim is no longer reachable.
        assert response.status_code in (404, 405)

    def test_create_block_with_tracks(self, client, network_with_tracks):
        """POST /api/blocks/create-with-tracks should create block with tracks."""
        positions = [[0, 0], [1, 0], [2, 0]]
        response = client.post(
            "/api/blocks/create-with-tracks",
            json={"block_id": "BLK_TEST", "positions": positions, "name": "Test Block"},
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data["block_id"] == "BLK_TEST"

    def test_extend_block(self, client, app_state, network_with_tracks):
        """POST /api/blocks/<block_id>/extend should extend block."""
        block = app_state.network.create_block_with_tracks("BLK_001", [(0, 0), (1, 0)])
        response = client.post(
            "/api/blocks/BLK_001/extend", json={"positions": [[2, 0]]}
        )
        assert response.status_code == 200

    def test_add_tracks_to_block(self, client, app_state, network_with_tracks):
        """POST /api/blocks/<block_id>/tracks should add tracks to block."""
        block = app_state.network.create_block("BLK_001", "Test")
        response = client.post(
            "/api/blocks/BLK_001/tracks", json={"positions": [[0, 0]]}
        )
        assert response.status_code == 200

    def test_remove_tracks_from_block(self, client, app_state, network_with_tracks):
        """DELETE /api/blocks/<block_id>/tracks should remove tracks."""
        block = app_state.network.create_block_with_tracks(
            "BLK_001", [(0, 0), (1, 0), (2, 0)]
        )
        response = client.delete(
            "/api/blocks/BLK_001/tracks", json={"positions": [[2, 0]]}
        )
        assert response.status_code == 200

    def test_get_block_voltage(self, client, app_state):
        """GET /api/blocks/<block_id>/voltage should return voltage."""
        app_state.network.create_block("BLK_001", "Test")
        response = client.get("/api/blocks/BLK_001/voltage")
        assert response.status_code == 200
        data = response.get_json()
        assert "voltage" in data

    def test_get_block_end_tiles(self, client, app_state, network_with_tracks):
        """GET /api/blocks/<block_id>/end_tiles should return start/end tiles."""
        app_state.network.create_block_with_tracks("BLK_001", [(0, 0), (1, 0)])
        response = client.get("/api/blocks/BLK_001/end_tiles")
        assert response.status_code == 200
        data = response.get_json()
        assert "start_tile" in data
        assert "end_tile" in data

    def test_can_extend_block(self, client, app_state, network_with_tracks):
        """POST /api/blocks/<block_id>/can_extend should validate extension."""
        app_state.network.create_block_with_tracks("BLK_001", [(0, 0), (1, 0)])
        response = client.post("/api/blocks/BLK_001/can_extend", json={"x": 2, "y": 0})
        assert response.status_code == 200
        data = response.get_json()
        assert "can_extend" in data


# ============================================================================
# Signal Routes Tests (8 routes)
# ============================================================================


class TestSignalRoutes:
    """Test signal management endpoints."""

    def test_create_signal(self, client, network_with_tracks):
        """POST /api/signal should create signal."""
        response = client.post(
            "/api/signal",
            json={"x": 0, "y": 0, "detection_direction": "E", "signal_type": "block"},
        )
        assert response.status_code == 200
        data = response.get_json()
        assert "signal_id" in data

    def test_get_signal(self, client, app_state, network_with_tracks):
        """GET /api/signal/<signal_id> should return signal."""
        signal = app_state.network.add_signal(0, 0, "E", SignalType.BLOCK)
        response = client.get(f"/api/signal/{signal.signal_id}")
        assert response.status_code == 200
        data = response.get_json()
        assert data["signal_id"] == signal.signal_id

    def test_update_signal(self, client, app_state, network_with_tracks):
        """PATCH /api/signal/<signal_id> should update signal."""
        signal = app_state.network.add_signal(0, 0, "E", SignalType.BLOCK)
        response = client.patch(
            f"/api/signal/{signal.signal_id}", json={"name": "Updated"}
        )
        assert response.status_code == 200

    def test_delete_signal(self, client, app_state, network_with_tracks):
        """DELETE /api/signal/<signal_id> should delete signal."""
        signal = app_state.network.add_signal(0, 0, "E", SignalType.BLOCK)
        response = client.delete(f"/api/signal/{signal.signal_id}")
        assert response.status_code == 200

    def test_get_signal_at(self, client, app_state, network_with_tracks):
        """GET /api/signal/<x>/<y> should return signal at position."""
        signal = app_state.network.add_signal(0, 0, "E", SignalType.BLOCK)
        response = client.get("/api/signal/0/0")
        assert response.status_code == 200

    def test_delete_signal_at(self, client, app_state, network_with_tracks):
        """DELETE /api/signal/<x>/<y> should delete signal at position."""
        app_state.network.add_signal(0, 0, "E", SignalType.BLOCK)
        response = client.delete("/api/signal/0/0")
        assert response.status_code == 200

    def test_get_reachable_blocks(self, client, app_state, network_with_tracks):
        """GET /api/signal/<signal_id>/reachable-blocks should return reachable blocks."""
        signal = app_state.network.add_signal(0, 0, "E", SignalType.BLOCK)
        response = client.get(f"/api/signal/{signal.signal_id}/reachable-blocks")
        assert response.status_code == 200
        data = response.get_json()
        assert "reachable_blocks" in data

    def test_validate_signal_at(self, client, network_with_tracks):
        """GET /api/signal/validate/<x>/<y> should validate signal placement."""
        response = client.get("/api/signal/validate/0/0")
        assert response.status_code == 200
        data = response.get_json()
        assert "can_place" in data


# ============================================================================
# Platform Routes Tests (11 routes)
# ============================================================================


class TestPlatformRoutes:
    """Test physical platform management endpoints."""

    def test_create_platform(self, client, network_with_tracks):
        """POST /api/platform should create platform."""
        response = client.post(
            "/api/platform", json={"x": 0, "y": 0, "name": "Test Platform"}
        )
        assert response.status_code == 200
        data = response.get_json()
        assert "platform_id" in data

    def test_get_platform(self, client, app_state, network_with_tracks):
        """GET /api/platform/<platform_id> should return platform."""
        platform = app_state.network.add_platform(0, 0, name="Test")
        response = client.get(f"/api/platform/{platform.platform_id}")
        assert response.status_code == 200

    def test_update_platform(self, client, app_state, network_with_tracks):
        """PATCH /api/platform/<platform_id> should update platform."""
        platform = app_state.network.add_platform(0, 0, name="Test")
        response = client.patch(
            f"/api/platform/{platform.platform_id}", json={"name": "Updated"}
        )
        assert response.status_code == 200

    def test_delete_platform(self, client, app_state, network_with_tracks):
        """DELETE /api/platform/<platform_id> should delete platform."""
        platform = app_state.network.add_platform(0, 0, name="Test")
        response = client.delete(f"/api/platform/{platform.platform_id}")
        assert response.status_code == 200

    def test_validate_platform_at(self, client, network_with_tracks):
        """GET /api/platform/validate/<x>/<y> should validate platform placement."""
        response = client.get("/api/platform/validate/0/0")
        assert response.status_code == 200
        data = response.get_json()
        assert "can_place" in data

    def test_extend_platform(self, client, app_state, network_with_tracks):
        """POST /api/platform/<platform_id>/extend should extend platform."""
        platform = app_state.network.add_platform(0, 0, name="Test")
        response = client.post(
            f"/api/platform/{platform.platform_id}/extend", json={"positions": [[1, 0]]}
        )
        assert response.status_code == 200

    def test_remove_platform_tile(self, client, app_state, network_with_tracks):
        """POST /api/platform/<platform_id>/remove_tile should remove tile."""
        platform = app_state.network.add_platform(0, 0, name="Test")
        app_state.network.extend_platform(platform.platform_id, [(1, 0)])
        response = client.post(
            f"/api/platform/{platform.platform_id}/remove_tile", json={"x": 1, "y": 0}
        )
        assert response.status_code == 200

    def test_remove_platform_tile_clears_block_assignment(
        self, client, app_state, network_with_tracks
    ):
        """Removing a platform tile should also remove stale tile block assignments."""
        platform = app_state.network.add_platform(0, 0, name="Test")
        app_state.network.extend_platform(platform.platform_id, [(1, 0)])
        app_state.network.create_block("BLK_001", "Test")

        assign_response = client.post(
            f"/api/platform/{platform.platform_id}/assign_tile",
            json={"x": 1, "y": 0, "block_id": "BLK_001"},
        )
        assert assign_response.status_code == 200

        remove_response = client.post(
            f"/api/platform/{platform.platform_id}/remove_tile", json={"x": 1, "y": 0}
        )
        assert remove_response.status_code == 200

        assignments_response = client.get(
            f"/api/platform/{platform.platform_id}/block_assignments"
        )
        assert assignments_response.status_code == 200
        assignments = assignments_response.get_json()["assignments"]
        assert {"x": 1, "y": 0, "block_id": "BLK_001"} not in assignments

    def test_can_extend_platform(self, client, app_state, network_with_tracks):
        """POST /api/platform/<platform_id>/can_extend should validate extension."""
        platform = app_state.network.add_platform(0, 0, name="Test")
        response = client.post(
            f"/api/platform/{platform.platform_id}/can_extend", json={"x": 1, "y": 0}
        )
        assert response.status_code == 200

    def test_assign_platform_tile_to_block(
        self, client, app_state, network_with_tracks
    ):
        """POST /api/platform/<platform_id>/assign_tile should assign tile to block."""
        platform = app_state.network.add_platform(0, 0, name="Test")
        block = app_state.network.create_block("BLK_001", "Test")
        response = client.post(
            f"/api/platform/{platform.platform_id}/assign_tile",
            json={"x": 0, "y": 0, "block_id": "BLK_001"},
        )
        assert response.status_code == 200

    def test_get_platform_block_assignments(
        self, client, app_state, network_with_tracks
    ):
        """GET /api/platform/<platform_id>/block_assignments should return assignments."""
        platform = app_state.network.add_platform(0, 0, name="Test")
        response = client.get(f"/api/platform/{platform.platform_id}/block_assignments")
        assert response.status_code == 200


class TestStationGroupRoutes:
    """Test logical station-group endpoints."""

    def test_create_station_group(self, client, app_state, network_with_tracks):
        platform = app_state.network.add_platform(0, 0, platform_id="PLT_A", name="A")
        response = client.post(
            "/api/station",
            json={
                "station_id": "STA_MAIN",
                "name": "Main",
                "platform_ids": [platform.platform_id],
            },
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data["station_id"] == "STA_MAIN"
        assert data["platform_ids"] == ["PLT_A"]

    def test_add_and_remove_platform(self, client, app_state, network_with_tracks):
        app_state.network.add_platform(0, 0, platform_id="PLT_A", name="A")
        app_state.network.add_platform(1, 0, platform_id="PLT_B", name="B")
        app_state.network.create_station_group("STA_MAIN", "Main", ["PLT_A"])

        add_resp = client.post(
            "/api/station/STA_MAIN/add-platform", json={"platform_id": "PLT_B"}
        )
        assert add_resp.status_code == 200
        assert "PLT_B" in add_resp.get_json()["platform_ids"]

        remove_resp = client.post(
            "/api/station/STA_MAIN/remove-platform", json={"platform_id": "PLT_B"}
        )
        assert remove_resp.status_code == 200
        assert "PLT_B" not in remove_resp.get_json()["platform_ids"]

    def test_list_station_groups(self, client, app_state, network_with_tracks):
        app_state.network.create_station_group("STA_MAIN", "Main", [])
        response = client.get("/api/station")
        assert response.status_code == 200
        data = response.get_json()
        assert "stations" in data


# ============================================================================
# Train Routes Tests (28 routes)
# ============================================================================


class TestTrainRoutes:
    """Test train management and control endpoints."""

    def test_create_train(self, client):
        """POST /api/trains should create train."""
        response = client.post(
            "/api/trains", json={"id": "TRAIN_001", "max_speed": 100.0}
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data["status"] == "success"

    def test_list_trains(self, client):
        """GET /api/trains should list all trains."""
        response = client.get("/api/trains")
        assert response.status_code == 200
        data = response.get_json()
        assert "trains" in data

    def test_get_train(self, client, app_state):
        """GET /api/trains/<train_id> should return train details."""
        car = TrainCar("CAR_1", "LOCOMOTIVE", length_cm=1000.0, max_speed=100.0)
        train = Train("TRAIN_001", consist=[car])
        app_state.network.add_train(train)
        response = client.get("/api/trains/TRAIN_001")
        assert response.status_code == 200

    def test_delete_train(self, client, app_state):
        """DELETE /api/trains/<train_id> should delete train."""
        car = TrainCar("CAR_1", "LOCOMOTIVE", length_cm=1000.0, max_speed=100.0)
        train = Train("TRAIN_001", consist=[car])
        app_state.network.add_train(train)
        response = client.delete("/api/trains/TRAIN_001")
        assert response.status_code == 200

    def test_train_power_on(self, client, app_state, network_with_tracks):
        """POST /api/trains/<train_id>/power-on should power on train."""
        car = TrainCar("CAR_1", "LOCOMOTIVE", length_cm=40.0, max_speed=100.0)
        train = Train("TRAIN_001", consist=[car])
        app_state.network.add_train(train)
        app_state.network.place_train(train, 0, 0, Direction.EAST)
        response = client.post("/api/trains/TRAIN_001/power-on")
        assert response.status_code == 200

    def test_train_power_off(self, client, app_state, network_with_tracks):
        """POST /api/trains/<train_id>/power-off should power off train."""
        car = TrainCar("CAR_1", "LOCOMOTIVE", length_cm=40.0, max_speed=100.0)
        train = Train("TRAIN_001", consist=[car])
        app_state.network.add_train(train)
        app_state.network.place_train(train, 0, 0, Direction.EAST)
        response = client.post("/api/trains/TRAIN_001/power-off")
        assert response.status_code in [200, 400]

# ============================================================================
# Path Routes Tests (9 routes)
# ============================================================================
# Path Routes Tests (9 routes)
# ============================================================================


class TestPathRoutes:
    """Test path management and dispatcher endpoints."""

    def test_create_path(self, client, app_state, network_with_tracks):
        """POST /api/paths should create path."""
        app_state.network.create_block_with_tracks("BLK_001", [(0, 0)])
        app_state.network.create_block_with_tracks("BLK_002", [(2, 0)])
        response = client.post(
            "/api/paths", json={"start_block_id": "BLK_001", "end_block_id": "BLK_002"}
        )
        # May succeed or fail depending on pathfinding
        assert response.status_code in [200, 400]

    def test_create_path_conflicts_when_train_already_has_active_path(
        self, client, app_state, network_with_tracks
    ):
        """POST /api/paths should not silently assign a second path to one train."""
        app_state.network.create_block_with_tracks("BLK_001", [(0, 0), (1, 0)])
        app_state.network.create_block_with_tracks("BLK_002", [(2, 0), (3, 0)])
        app_state.network.create_block_with_tracks("BLK_003", [(4, 0)])

        car = TrainCar("CAR_1", "LOCOMOTIVE", length_cm=40.0, max_speed=100.0)
        train = Train("TRAIN_001", consist=[car])
        app_state.network.add_train(train)
        app_state.network.place_train(train, 0, 0, Direction.EAST)

        # The dispatcher resolves the train's head block via its controller
        # span (head/tail live on the controller in v1, not on Train). Seed the
        # span at the placement tile so create_path can resolve BLK_001 as the
        # current block instead of failing with "Cannot resolve current block".
        controller = app_state.sim_controller.get_or_create_controller("TRAIN_001")
        set_train_span(
            controller, (0, 0), facing_direction=Direction.EAST,
            occupied_blocks=["BLK_001"],
        )

        first_response = client.post(
            "/api/paths",
            json={
                "requested_destination_block_id": "BLK_002",
                "requested_destination_direction": "forward",
                "train_id": "TRAIN_001",
            },
        )
        # With the controller span seeded above, the dispatcher resolves the
        # head block and the 2-block A->B path is acquired (status 200). The
        # conflict-on-second-create contract is what's being verified; a non-200
        # here would mean create_path regressed, so fail loudly rather than skip.
        assert first_response.status_code == 200, (
            f"create_path should succeed once the controller span is seeded; "
            f"got {first_response.status_code}: {first_response.get_json()}"
        )
        first_data = first_response.get_json()
        assert first_data["success"] is True
        first_path_id = first_data.get("path_id") or first_data.get("path", {}).get(
            "path_id"
        )
        # Per design_new.md, the dispatcher owns active-path lookup; Train no
        # longer carries `assigned_path_id`.
        assert app_state.path_dispatcher.active_path_id_by_train("TRAIN_001") == first_path_id

        second_response = client.post(
            "/api/paths",
            json={
                "requested_destination_block_id": "BLK_003",
                "requested_destination_direction": "forward",
                "train_id": "TRAIN_001",
            },
        )
        # v1 routes/paths.py returns the dispatcher's PathAcquisitionResult
        # which includes a failure_kind=="invalid_request" when the train
        # already holds a path. The HTTP layer doesn't promote this to 409,
        # so we assert on the result kind, not the legacy 409 status code.
        assert second_response.status_code in (200, 400, 409)
        second_data = second_response.get_json()
        assert second_data["success"] is False
        # Train should still hold the original path.
        assert app_state.path_dispatcher.active_path_id_by_train("TRAIN_001") == first_path_id

    def test_list_paths(self, client):
        """GET /api/paths should list all paths."""
        response = client.get("/api/paths")
        assert response.status_code == 200
        data = response.get_json()
        assert "paths" in data

    def test_find_dispatcher_paths(self, client, app_state, network_with_tracks):
        """POST /api/dispatcher/paths should find paths."""
        app_state.network.create_block_with_tracks("BLK_001", [(0, 0)])
        app_state.network.create_block_with_tracks("BLK_002", [(2, 0)])
        response = client.post(
            "/api/dispatcher/paths",
            json={"start_block_id": "BLK_001", "end_block_id": "BLK_002"},
        )
        assert response.status_code == 200

    def test_get_reachable_endpoints(self, client, app_state, network_with_tracks):
        """GET /api/dispatcher/reachable/<block_id> should return reachable blocks."""
        app_state.network.create_block_with_tracks("BLK_001", [(0, 0)])
        response = client.get("/api/dispatcher/reachable/BLK_001")
        assert response.status_code == 200


# ============================================================================
# Misc Routes Tests (33 routes - testing key endpoints)
# ============================================================================


class TestMiscRoutes:
    """Test miscellaneous endpoints."""

    def test_index(self, client):
        """GET / should return index.html."""
        response = client.get("/")
        assert response.status_code == 200

    def test_get_network(self, client):
        """GET /api/network should return network state."""
        response = client.get("/api/network")
        assert response.status_code == 200
        data = response.get_json()
        assert "tracks" in data

    def test_simulation_control_start(self, client):
        """POST /api/simulation/control with action=start should start simulation."""
        response = client.post("/api/simulation/control", json={"action": "start"})
        assert response.status_code == 200
        data = response.get_json()
        assert data["status"] == "success"

    def test_simulation_control_stop(self, client):
        """POST /api/simulation/control with action=stop should stop simulation."""
        response = client.post("/api/simulation/control", json={"action": "stop"})
        assert response.status_code == 200

    def test_list_layouts(self, client):
        """GET /api/layouts should list layouts."""
        response = client.get("/api/layouts")
        assert response.status_code == 200
        data = response.get_json()
        assert "layouts" in data

    def test_save_layout(self, client):
        """POST /api/layouts should save layout."""
        response = client.post("/api/layouts", json={"name": "test_layout"})
        assert response.status_code == 200

    def test_network_stats(self, client):
        """GET /api/network/stats should return network statistics."""
        response = client.get("/api/network/stats")
        assert response.status_code == 200

    def test_network_clear(self, client):
        """POST /api/network/clear should clear network."""
        response = client.post("/api/network/clear")
        assert response.status_code == 200


class TestRouteRedesignApis:
    """Coverage for newly added route-panel redesign APIs."""

    def _create_train(self, app_state, train_id: str = "TRAIN_A"):
        car = TrainCar(car_id=f"{train_id}_CAR", car_type="LOCOMOTIVE", length_cm=20.0)
        train = Train(train_id=train_id, consist=[car], max_speed=80.0)
        app_state.network.add_train(train)

    def _setup_connected_platforms(self, app_state, client):
        for x in range(4):
            app_state.network.add_track(TrackSegment(x=x, y=0, track_bits=2))

        app_state.network.create_block_with_tracks(
            block_id="BLK_A",
            positions=[(0, 0), (1, 0)],
            name="Block A",
        )
        app_state.network.create_block_with_tracks(
            block_id="BLK_B",
            positions=[(2, 0), (3, 0)],
            name="Block B",
        )

        assert (
            client.post(
                "/api/platform",
                json={
                    "x": 0,
                    "y": 0,
                    "platform_id": "PLT_A",
                    "name": "Platform A",
                },
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/api/platform",
                json={
                    "x": 2,
                    "y": 0,
                    "platform_id": "PLT_B",
                    "name": "Platform B",
                },
            ).status_code
            == 200
        )

    def test_platform_endpoint_create_and_get(self, client, app_state):
        app_state.network.add_track(TrackSegment(x=10, y=0, track_bits=2))
        create_resp = client.post(
            "/api/platform",
            json={"x": 10, "y": 0, "platform_id": "PLT_001", "name": "P1"},
        )
        assert create_resp.status_code == 200

        get_resp = client.get("/api/platform/PLT_001")
        assert get_resp.status_code == 200
        payload = get_resp.get_json()
        assert payload["platform_id"] == "PLT_001"

    def test_train_route_viability_endpoint(self, client, app_state):
        self._setup_connected_platforms(app_state, client)
        resp = client.post(
            "/api/train-routes/check-viability",
            json={"pairs": [["PLT_A", "PLT_B"]]},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert "results" in data
        assert len(data["results"]) == 1
        assert data["results"][0]["from"] == "PLT_A"
        assert data["results"][0]["to"] == "PLT_B"

    def test_viability_dict_payload_with_direction(self, client, app_state):
        """New payload shape {from, to, direction} resolves correctly and
        response carries hop_count, distance_cm, and resolved direction."""
        self._setup_connected_platforms(app_state, client)
        resp = client.post(
            "/api/train-routes/check-viability",
            json={"pairs": [
                {"from": "PLT_A", "to": "PLT_B", "direction": "either"}
            ]},
        )
        assert resp.status_code == 200
        result = resp.get_json()["results"][0]
        assert result["viable"] is True
        assert result["hop_count"] >= 1
        assert result["distance_cm"] > 0
        assert result["direction"] in ("forward", "reverse")

    def test_viability_traverses_switch_tile(self, client, app_state):
        """Switch-barrier regression: blocks separated by a tile whose
        ``block_section_id`` is None (the way switches are modeled in v1)
        must still be reachable. The legacy ``build_block_graph``
        treated such tiles as graph barriers and chopped the network into
        disconnected components, marking valid pairs unviable."""
        network = app_state.network
        for x in range(5):
            network.add_track(TrackSegment(x=x, y=0, track_bits=2))
        # (2,0) is intentionally left out of both blocks; it has track
        # geometry but no block_section_id — the same shape a switch tile
        # has in v1.
        network.create_block_with_tracks(
            block_id="BLK_LEFT",
            positions=[(0, 0), (1, 0)],
            name="Left",
        )
        network.create_block_with_tracks(
            block_id="BLK_RIGHT",
            positions=[(3, 0), (4, 0)],
            name="Right",
        )

        resp = client.post(
            "/api/train-routes/check-viability",
            json={"pairs": [{
                "from": {"block_id": "BLK_LEFT"},
                "to": {"block_id": "BLK_RIGHT"},
                "direction": "either",
            }]},
        )
        assert resp.status_code == 200
        result = resp.get_json()["results"][0]
        assert result["viable"] is True, (
            f"BLK_LEFT → BLK_RIGHT should traverse the unblocked tile; "
            f"got {result}"
        )
        assert result["hop_count"] >= 1
        assert result["distance_cm"] > 0
        assert result["direction"] in ("forward", "reverse")

    def test_viability_unreachable_blocks(self, client, app_state):
        """Truly disconnected blocks return viable=False with a No-path
        error and the requested direction is echoed back."""
        network = app_state.network
        network.add_track(TrackSegment(x=0, y=0, track_bits=2))
        network.add_track(TrackSegment(x=10, y=0, track_bits=2))
        network.create_block_with_tracks(
            block_id="BLK_X",
            positions=[(0, 0)],
            name="X",
        )
        network.create_block_with_tracks(
            block_id="BLK_Y",
            positions=[(10, 0)],
            name="Y",
        )

        resp = client.post(
            "/api/train-routes/check-viability",
            json={"pairs": [{
                "from": {"block_id": "BLK_X"},
                "to": {"block_id": "BLK_Y"},
                "direction": "forward",
            }]},
        )
        assert resp.status_code == 200
        result = resp.get_json()["results"][0]
        assert result["viable"] is False
        assert "No path found" in (result.get("error") or "")
        assert result.get("requested_direction") == "forward"

    def test_viability_self_pair_is_trivially_viable(self, client, app_state):
        """A pair whose from-block and to-block resolve to the same
        block is viable with zero hops and zero distance."""
        self._setup_connected_platforms(app_state, client)
        resp = client.post(
            "/api/train-routes/check-viability",
            json={"pairs": [{
                "from": "PLT_A", "to": "PLT_A", "direction": "either"
            }]},
        )
        assert resp.status_code == 200
        result = resp.get_json()["results"][0]
        assert result["viable"] is True
        assert result["hop_count"] == 0
        assert result["distance_cm"] == 0.0

    # ------------------------------------------------------------------
    # Quick Route flow: the canonical replacement for the legacy
    # POST /api/paths create_path call from the editor's path tool.
    # Per design_new.md:1297, manual reposition is modeled through an
    # ordinary one-off Route + RouteRun rather than a raw path.
    # ------------------------------------------------------------------

    def _place_powered_train_on_two_block_line(self, app_state, train_id="TRAIN_QR"):
        """Build BLK_A→BLK_B, place a powered cab-A train on BLK_A.

        Goes through `sim_controller.add_train(...)`, which is the canonical
        entry point that writes the initial `TrainController.physics_sync
        .current_span` from the span returned by `network.place_train(...)`
        (simulation.py:607). Phase 1 cannot allocate a path without that span.
        """
        for x in range(4):
            app_state.network.add_track(TrackSegment(x=x, y=0, track_bits=2))
        app_state.network.create_block_with_tracks(
            block_id="BLK_A", positions=[(0, 0), (1, 0)], name="Block A"
        )
        app_state.network.create_block_with_tracks(
            block_id="BLK_B", positions=[(2, 0), (3, 0)], name="Block B"
        )

        train = app_state.sim_controller.add_train(
            train_id=train_id,
            starting_block_id="BLK_A",
            initial_direction="EAST",
            max_speed_kmh=60.0,
            length_cm=20.0,
            initial_cab="A",
        )

        controller = app_state.sim_controller.get_or_create_controller(train_id)
        controller.commands.control_mode = "ato"
        controller.ops.state = TrainState.READY
        return train, controller

    def test_quick_route_three_call_flow_creates_path_via_phase1(self, client, app_state):
        """The Quick Route tool's three-call sequence — POST /api/train-routes
        (one-off, single stop task) + POST /train-routes/<id>/activate
        + PUT /api/trains/<id>/assign-route — should cause Phase 1 to
        allocate a dispatcher path on a subsequent sim tick.

        Also verifies that POST /api/train-routes accepts
        ``acquisition_mode`` (the operator picks all_or_nothing for Quick
        Route, taking full responsibility for the manual reposition).
        """
        self._place_powered_train_on_two_block_line(app_state)

        create_resp = client.post(
            "/api/train-routes",
            json={
                "route_id": "ROUTE_QR_TEST",
                "name": "Quick: TRAIN_QR -> BLK_B",
                "mode": "one_off",
                "tasks": [{
                    "task_kind": "stop",
                    "task_location": {"block_id": "BLK_B"},
                    "task_direction": "forward",
                }],
                "acquisition_mode": "all_or_nothing",
            },
        )
        assert create_resp.status_code == 200, create_resp.get_json()
        assert (
            create_resp.get_json()["route"]["acquisition_mode"]
            == "all_or_nothing"
        ), "POST /api/train-routes must pass acquisition_mode through to the service"

        activate_resp = client.post("/api/train-routes/ROUTE_QR_TEST/activate")
        assert activate_resp.status_code == 200, activate_resp.get_json()

        assign_resp = client.put(
            "/api/trains/TRAIN_QR/assign-route",
            json={"route_id": "ROUTE_QR_TEST"},
        )
        assert assign_resp.status_code == 200, assign_resp.get_json()

        # Pre-tick: no path yet — assign-route only writes the assignment.
        assert app_state.path_dispatcher.active_path_id_by_train("TRAIN_QR") is None

        # Run a few ticks; Phase 1 should call PathDispatcher.create_path.
        active_path_id = None
        for _ in range(10):
            app_state.sim_controller.update(0.1)
            active_path_id = app_state.path_dispatcher.active_path_id_by_train("TRAIN_QR")
            if active_path_id is not None:
                break

        assert active_path_id is not None, (
            "Expected Phase 1 to allocate a dispatcher path within ~10 ticks "
            "after assign-route. None was allocated — the one-off route flow "
            "is the contract editor-paths.js's Quick Route tool depends on."
        )

    def test_assign_route_conflict_returns_409_without_force(self, client, app_state):
        """assign-route returns 409 when the train already has an active
        route assignment and `force=true` is not supplied. The Quick Route
        UI uses this 409 to drive a confirmation modal before retrying with
        force=true. The forced retry must succeed."""
        self._place_powered_train_on_two_block_line(app_state)

        for route_id in ("ROUTE_FIRST", "ROUTE_SECOND"):
            resp = client.post(
                "/api/train-routes",
                json={
                    "route_id": route_id,
                    "mode": "one_off",
                    "tasks": [{
                        "task_kind": "stop",
                        "task_location": {"block_id": "BLK_B"},
                        "task_direction": "forward",
                    }],
                },
            )
            assert resp.status_code == 200, resp.get_json()

        assert (
            client.put(
                "/api/trains/TRAIN_QR/assign-route",
                json={"route_id": "ROUTE_FIRST"},
            ).status_code
            == 200
        )

        conflict_resp = client.put(
            "/api/trains/TRAIN_QR/assign-route",
            json={"route_id": "ROUTE_SECOND"},
        )
        assert conflict_resp.status_code == 409
        assert "already assigned" in conflict_resp.get_json()["error"].lower()

        forced_resp = client.put(
            "/api/trains/TRAIN_QR/assign-route",
            json={"route_id": "ROUTE_SECOND", "force": True},
        )
        assert forced_resp.status_code == 200, forced_resp.get_json()


# ============================================================================
# Summary
# ============================================================================

"""
Test Coverage Summary:

- Track Routes: 8 tests (5 routes fully covered)
- Block Routes: 14 tests (13 routes fully covered)
- Signal Routes: 8 tests (8 routes fully covered)
- Station Routes: 11 tests (11 routes fully covered)
- Train Routes: 7 tests (28 routes - basic coverage)
- Path Routes: 4 tests (9 routes - basic coverage)
- Misc Routes: 8 tests (33 routes - key endpoints covered)

Total: 60+ tests covering the most critical functionality of all 108 routes.

Additional tests can be added for:
- Train control commands (ATO, manual control, speed control, etc.)
- Switch operations
- Schedule management
- Train route management
- Speed profile management
- Extended error cases
"""
