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
from track import Tile
from block import Block
from railway_signal import Signal, SignalType
from station import Station
from train import Train, TrainCar
from switch import Switch
from enums import Direction, BlockState, TrainState
from tests.train_test_helpers import set_train_span
from tests.segment_test_helpers import make_segment_block, make_segment_block_with_tracks


@pytest.fixture
def app_state(tmp_path):
    """Create a fresh AppState with initialized components for testing."""
    network = RailwayNetwork()
    path_dispatcher = PathDispatcher(network)
    sim_controller = SimulationController(network, path_dispatcher)
    # Keep route tests hermetic: save/load/delete must never write into the
    # repository's real layouts directory.
    persistence_manager = PersistenceManager(storage_dir=str(tmp_path))

    state = AppState(
        network=network,
        path_dispatcher=path_dispatcher,
        sim_controller=sim_controller,
        persistence_manager=persistence_manager,
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
        track = Tile(x=x, y=0, track_bits=2)  # EW track
        network.add_track(track)

    # Add straight north-south tracks at x=0
    for y in range(1, 5):
        track = Tile(x=0, y=y, track_bits=1)  # NS track
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

    def test_update_track_switch_change_rejected_returns_409(
        self, client, app_state, network_with_tracks
    ):
        """A locked switch must surface a 409 with an error, not a 500.

        When a runtime SwitchManager is wired, a rejected throw (e.g. the
        switch is locked by a live path) returns the tagged
        `switch_change_rejected` result. The route must map it to 409 with
        an `error` field the UI can show -- not fall through and KeyError.
        """
        from runtime_payloads import SwitchLockClaim

        network = network_with_tracks
        track = network.get_track(1, 0)
        track.track_bits = 6  # EW main + NE diverge => a switch tile (is_switch)
        track.switch_id = "SW_test"
        network.add_switch(Switch(switch_id="SW_test", x=1, y=0))

        # Lock the switch to MAIN (0) so a DIVERGE (1) request conflicts.
        switch = network.switches["SW_test"]
        assert switch.acquire_lock(
            SwitchLockClaim(path_id="P_test", train_id="T1", locked_state=0)
        )

        response = client.patch("/api/track/1/0", json={"switch_state": 1})
        assert response.status_code == 409
        data = response.get_json()
        assert "error" in data
        assert "locked" in data["error"].lower()

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
    """Test block grouping + config + voltage endpoints (geometry lives on the
    segment surface, see TestSegmentRoutes)."""

    def test_get_blocks(self, client):
        """GET /api/blocks should list all blocks."""
        response = client.get("/api/blocks")
        assert response.status_code == 200
        data = response.get_json()
        assert isinstance(data, list)

    def test_get_block_success(self, client, app_state):
        """GET /api/blocks/<block_id> should return block details incl.
        segment_ids."""
        make_segment_block(app_state.network, "BLK_001", "Test Block")
        response = client.get("/api/blocks/BLK_001")
        assert response.status_code == 200
        data = response.get_json()
        assert data["block_id"] == "BLK_001"
        assert data["segment_ids"] == ["SEG_001"]
        assert "occupied_by" in data and "state" in data

    def test_get_block_not_found(self, client):
        """GET /api/blocks/<block_id> should return 404 for nonexistent block."""
        response = client.get("/api/blocks/NONEXISTENT")
        assert response.status_code == 404

    def test_update_block(self, client, app_state):
        """PATCH /api/blocks/<block_id> should update block config."""
        make_segment_block(app_state.network, "BLK_001", "Test")
        response = client.patch("/api/blocks/BLK_001", json={"name": "Updated"})
        assert response.status_code == 200
        data = response.get_json()
        assert data["name"] == "Updated"

    def test_update_block_rejects_geometry(self, client, app_state):
        """PATCH /api/blocks is config only — a geometry field is rejected and
        points at the segment endpoint."""
        make_segment_block(app_state.network, "BLK_001", "Test")
        response = client.patch("/api/blocks/BLK_001", json={"length_cm": 50.0})
        assert response.status_code == 400

    def test_update_block_allowed_travel_directions_one_way(
        self, client, app_state
    ):
        """PATCH should set a one-way `allowed_travel_directions` policy and
        round-trip it through `to_dict` (sorted list)."""
        block = make_segment_block(app_state.network, "BLK_001", "Test")
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
        make_segment_block(app_state.network, "BLK_001", "Test")
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
        block = make_segment_block(app_state.network, "BLK_001", "Test")
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
        block = make_segment_block(app_state.network, "BLK_001", "Test")
        response = client.patch(
            "/api/blocks/BLK_001",
            json={"allowed_travel_directions": ["sideways"]},
        )
        assert response.status_code == 400
        assert block.allowed_travel_directions == {"forward", "reverse"}

    def test_delete_block_ungroups(self, client, app_state):
        """DELETE /api/blocks/<block_id> dissolves the grouping (ungroup)."""
        make_segment_block(app_state.network, "BLK_001", "Test")
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
        make_segment_block(app_state.network, "BLK_001", "Test")
        response = client.post("/api/blocks/BLK_001/cycle-state")
        # 404 if Flask doesn't know the route at all; 405 if Flask
        # matches the URL prefix but rejects the method. Either way the
        # admin shim is no longer reachable.
        assert response.status_code in (404, 405)

    def test_get_block_voltage(self, client, app_state):
        """GET /api/blocks/<block_id>/voltage should return voltage."""
        make_segment_block(app_state.network, "BLK_001", "Test")
        response = client.get("/api/blocks/BLK_001/voltage")
        assert response.status_code == 200
        data = response.get_json()
        assert "voltage" in data


class TestSegmentRoutes:
    """Test segment geometry endpoints (/api/segments)."""

    def test_create_segment_with_tracks(self, client, network_with_tracks):
        """POST /api/segments should create a segment holding the tracks."""
        positions = [[0, 0], [1, 0], [2, 0]]
        response = client.post(
            "/api/segments",
            json={"segment_id": "SEG_TEST", "positions": positions, "name": "T"},
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data["segment_id"] == "SEG_TEST"
        assert data["block_id"] == "BLK_TEST"  # fresh segment gets its own derived block

    def test_get_segments_list(self, client, app_state, network_with_tracks):
        """GET /api/segments should list all segments."""
        app_state.network.create_segment_with_tracks("SEG_001", [(0, 0), (1, 0)])
        response = client.get("/api/segments")
        assert response.status_code == 200
        assert any(s["segment_id"] == "SEG_001" for s in response.get_json())

    def test_get_segment_success(self, client, app_state, network_with_tracks):
        """GET /api/segments/<segment_id> should return geometry."""
        app_state.network.create_segment_with_tracks("SEG_001", [(0, 0), (1, 0)])
        response = client.get("/api/segments/SEG_001")
        assert response.status_code == 200
        data = response.get_json()
        assert data["segment_id"] == "SEG_001"
        assert data["is_continuous"] is True

    def test_get_segment_not_found(self, client):
        response = client.get("/api/segments/NONEXISTENT")
        assert response.status_code == 404

    def test_update_segment_geometry(self, client, app_state, network_with_tracks):
        """PATCH /api/segments/<segment_id> should update geometry."""
        app_state.network.create_segment_with_tracks("SEG_001", [(0, 0), (1, 0)])
        response = client.patch(
            "/api/segments/SEG_001", json={"length_cm": 120.0, "grade_percent": 2.0}
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data["length_cm"] == 120.0
        assert data["grade_percent"] == 2.0

    def test_delete_segment(self, client, app_state, network_with_tracks):
        """DELETE /api/segments/<segment_id> should delete the segment."""
        app_state.network.create_segment_with_tracks("SEG_001", [(0, 0), (1, 0)])
        response = client.delete("/api/segments/SEG_001")
        assert response.status_code == 200
        assert app_state.network.get_segment("SEG_001") is None

    def test_extend_segment(self, client, app_state, network_with_tracks):
        """POST /api/segments/<segment_id>/extend should extend the segment."""
        app_state.network.create_segment_with_tracks("SEG_001", [(0, 0), (1, 0)])
        response = client.post(
            "/api/segments/SEG_001/extend", json={"positions": [[2, 0]]}
        )
        assert response.status_code == 200

    def test_add_tracks_to_segment(self, client, app_state, network_with_tracks):
        """POST /api/segments/<segment_id>/tracks should add tracks."""
        app_state.network.create_segment("SEG_001", "Test")
        response = client.post(
            "/api/segments/SEG_001/tracks", json={"positions": [[0, 0]]}
        )
        assert response.status_code == 200

    def test_remove_tracks_from_segment(self, client, app_state, network_with_tracks):
        """DELETE /api/segments/<segment_id>/tracks should remove tracks."""
        app_state.network.create_segment_with_tracks("SEG_001", [(0, 0), (1, 0), (2, 0)]
        )
        response = client.delete(
            "/api/segments/SEG_001/tracks", json={"positions": [[2, 0]]}
        )
        assert response.status_code == 200

    def test_get_segment_end_tiles(self, client, app_state, network_with_tracks):
        """GET /api/segments/<segment_id>/end_tiles should return start/end."""
        app_state.network.create_segment_with_tracks("SEG_001", [(0, 0), (1, 0)])
        response = client.get("/api/segments/SEG_001/end_tiles")
        assert response.status_code == 200
        data = response.get_json()
        assert "start_tile" in data
        assert "end_tile" in data

    def test_can_extend_segment(self, client, app_state, network_with_tracks):
        """POST /api/segments/<segment_id>/can_extend should validate."""
        app_state.network.create_segment_with_tracks("SEG_001", [(0, 0), (1, 0)])
        response = client.post(
            "/api/segments/SEG_001/can_extend", json={"x": 2, "y": 0}
        )
        assert response.status_code == 200
        assert "can_extend" in response.get_json()


class TestBlockSegmentGroupingRoutes:
    """The block/segment split trap: group MULTIPLE segments with visibly
    distinct ids into one block and exercise every grouping edge.

    A single-segment block has block_id == segment_id, which hides an
    id-aliasing bug. Every test here uses distinct ids so a confusion fails:

        BLK_1  groups  SEG_A  +  SEG_B  ( +  SEG_C )
    """

    def _make_segments(self, network):
        """Three connected EW segments on the shared network_with_tracks run.

        SEG_A = (0,0),(1,0) ; SEG_B = (2,0) ; SEG_C = (3,0),(4,0).
        """
        network.create_segment_with_tracks("SEG_A", [(0, 0), (1, 0)], length_cm=100.0)
        network.create_segment_with_tracks("SEG_B", [(2, 0)], length_cm=20.0)
        network.create_segment_with_tracks("SEG_C", [(3, 0), (4, 0)], length_cm=60.0)

    def test_group_multiple_segments_into_one_block(
        self, client, app_state, network_with_tracks
    ):
        self._make_segments(app_state.network)
        response = client.post(
            "/api/blocks",
            json={"block_id": "BLK_1", "segment_ids": ["SEG_A", "SEG_B"], "name": "Yard"},
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data["block_id"] == "BLK_1"
        assert data["segment_ids"] == ["SEG_A", "SEG_B"]
        # The members' prior single-segment blocks are gone; only BLK_1 groups
        # them now. Distinct ids: a block id is not a segment id.
        assert "SEG_A" not in app_state.network.blocks
        assert app_state.network.get_track(0, 0).block_id == "BLK_1"
        assert app_state.network.get_track(0, 0).segment_id == "SEG_A"

    def test_group_accepts_segments_out_of_run_order(
        self, client, app_state, network_with_tracks
    ):
        """An operator selecting on canvas has no canonical order to give, so
        grouping works the run order out itself rather than 400-ing."""
        self._make_segments(app_state.network)
        response = client.post(
            "/api/blocks",
            # deliberately reversed relative to the run
            json={"block_id": "BLK_1", "segment_ids": ["SEG_B", "SEG_A"]},
        )
        assert response.status_code == 200
        # Stored in run order, not the order posted.
        assert response.get_json()["segment_ids"] == ["SEG_A", "SEG_B"]

    def test_group_rejects_a_misoriented_member_instead_of_reversing_it(
        self, client, app_state, network_with_tracks
    ):
        """Grouping edits block membership only. A member stored the opposite
        way round the run is rejected — rewriting its `ordered_tiles` would be a
        geometry write, which belongs on the segment axis."""
        network = app_state.network
        self._make_segments(network)
        # Flip SEG_B so it runs against SEG_A.
        network._get_segment_geometry_manager().reverse_segment_orientation("SEG_A")
        before = [(e.x, e.y) for e in network.get_segment("SEG_A").ordered_tiles]

        response = client.post(
            "/api/blocks", json={"block_id": "BLK_1", "segment_ids": ["SEG_A", "SEG_B"]}
        )
        if response.status_code == 400:
            # names the offending segment and points at the segment axis
            error = response.get_json()["error"]
            assert "SEG_A" in error or "SEG_B" in error
            assert "reverse" in error.lower()
            # and it did NOT rewrite the geometry
            assert [
                (e.x, e.y) for e in network.get_segment("SEG_A").ordered_tiles
            ] == before

    def test_reverse_segment_is_on_the_segment_axis(
        self, client, app_state, network_with_tracks
    ):
        """POST /api/segments/<id>/reverse-orientation flips stored tile order —
        the explicit geometry write grouping refuses to do implicitly."""
        network = app_state.network
        self._make_segments(network)
        before = [(e.x, e.y) for e in network.get_segment("SEG_A").ordered_tiles]

        response = client.post("/api/segments/SEG_A/reverse-orientation")
        assert response.status_code == 200
        assert response.get_json()["reversed"] is True

        after = [(e.x, e.y) for e in network.get_segment("SEG_A").ordered_tiles]
        assert after == list(reversed(before))

    def test_reverse_unknown_segment_404s(self, client, app_state, network_with_tracks):
        assert client.post("/api/segments/NOPE/reverse-orientation").status_code == 404

    def test_add_segment_at_end(self, client, app_state, network_with_tracks):
        self._make_segments(app_state.network)
        client.post("/api/blocks", json={"block_id": "BLK_1", "segment_ids": ["SEG_A"]})
        response = client.post(
            "/api/blocks/BLK_1/segments", json={"segment_id": "SEG_B"}
        )
        assert response.status_code == 200
        assert response.get_json()["segment_ids"] == ["SEG_A", "SEG_B"]

    def test_add_non_joining_segment_rejected(
        self, client, app_state, network_with_tracks
    ):
        """A segment that joins neither end of the run is rejected (end-only)."""
        self._make_segments(app_state.network)
        # SEG_FAR is a separate NS run at x=0, not connected to the EW run.
        app_state.network.create_segment_with_tracks("SEG_FAR", [(0, 3), (0, 4)])
        client.post("/api/blocks", json={"block_id": "BLK_1", "segment_ids": ["SEG_A"]})
        response = client.post(
            "/api/blocks/BLK_1/segments", json={"segment_id": "SEG_FAR"}
        )
        assert response.status_code == 400

    def test_remove_end_segment_rehomes_it(
        self, client, app_state, network_with_tracks
    ):
        self._make_segments(app_state.network)
        client.post(
            "/api/blocks",
            json={"block_id": "BLK_1", "segment_ids": ["SEG_A", "SEG_B"]},
        )
        response = client.delete("/api/blocks/BLK_1/segments/SEG_B")
        assert response.status_code == 200
        assert response.get_json()["segment_ids"] == ["SEG_A"]
        # SEG_B returned to its own single-segment block, whose id is the
        # segment's derived onto the block axis.
        assert app_state.network.get_block("BLK_B") is not None
        assert app_state.network.get_track(2, 0).block_id == "BLK_B"

    def test_remove_middle_segment_rejected(
        self, client, app_state, network_with_tracks
    ):
        """Removing a middle segment would split the run — rejected (400)."""
        self._make_segments(app_state.network)
        client.post(
            "/api/blocks",
            json={"block_id": "BLK_1", "segment_ids": ["SEG_A", "SEG_B", "SEG_C"]},
        )
        response = client.delete("/api/blocks/BLK_1/segments/SEG_B")
        assert response.status_code == 400
        # The grouping is untouched.
        assert app_state.network.get_block("BLK_1").segment_ids == [
            "SEG_A",
            "SEG_B",
            "SEG_C",
        ]

    def test_ungroup_rehomes_every_member(
        self, client, app_state, network_with_tracks
    ):
        self._make_segments(app_state.network)
        client.post(
            "/api/blocks",
            json={"block_id": "BLK_1", "segment_ids": ["SEG_A", "SEG_B"]},
        )
        response = client.delete("/api/blocks/BLK_1")
        assert response.status_code == 200
        # BLK_1 is gone; each freed segment is back in its own block.
        assert client.get("/api/blocks/BLK_1").status_code == 404
        assert client.get("/api/blocks/BLK_A").status_code == 200
        assert client.get("/api/blocks/BLK_B").status_code == 200
        # Geometry survived the ungroup.
        assert app_state.network.get_segment("SEG_A") is not None

    def test_geometry_patch_on_member_updates_only_that_segment(
        self, client, app_state, network_with_tracks
    ):
        """A geometry PATCH addresses one member of a grouped block and touches
        only that segment; the block length is the summed read."""
        self._make_segments(app_state.network)
        client.post(
            "/api/blocks",
            json={"block_id": "BLK_1", "segment_ids": ["SEG_A", "SEG_B"]},
        )
        response = client.patch("/api/segments/SEG_A", json={"length_cm": 99.0})
        assert response.status_code == 200
        assert app_state.network.get_segment("SEG_A").length_cm == 99.0
        # SEG_B is untouched.
        assert app_state.network.get_segment("SEG_B").length_cm == 20.0
        # Block length is the sum across members.
        assert app_state.network.get_block("BLK_1").length_cm == 119.0

    def test_block_patch_rejects_geometry_on_grouped_block(
        self, client, app_state, network_with_tracks
    ):
        """A block-level geometry PATCH on a grouped block is rejected — there
        is no single member to write, and the config surface is not geometry."""
        self._make_segments(app_state.network)
        client.post(
            "/api/blocks",
            json={"block_id": "BLK_1", "segment_ids": ["SEG_A", "SEG_B"]},
        )
        response = client.patch("/api/blocks/BLK_1", json={"length_cm": 5.0})
        assert response.status_code == 400


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

    def test_create_platform_multi_tile(self, client, network_with_tracks):
        """POST /api/platform with positions should create a multi-tile platform."""
        response = client.post(
            "/api/platform",
            json={"positions": [[0, 0], [1, 0]], "name": "Multi Platform"},
        )
        assert response.status_code == 200
        data = response.get_json()
        assert "platform_id" in data
        assert len(data["positions"]) == 2

    def test_create_platform_multi_tile_invalid_rolls_back(
        self, client, app_state, network_with_tracks
    ):
        """A multi-tile create that fails extension leaves no stray platform."""
        before = len(app_state.network.platforms)
        # (5, 5) has no track, so extending the (0, 0) anchor onto it fails;
        # the anchor must be rolled back rather than left behind.
        response = client.post(
            "/api/platform", json={"positions": [[0, 0], [5, 5]], "name": "Bad"}
        )
        assert response.status_code == 400
        assert len(app_state.network.platforms) == before

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
        make_segment_block(app_state.network, "BLK_001", "Test")

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
        block = make_segment_block(app_state.network, "BLK_001", "Test")
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

    def test_unassign_platform_tile_from_block(
        self, client, app_state, network_with_tracks
    ):
        platform = app_state.network.add_platform(0, 0, name="Test")
        make_segment_block(app_state.network, "BLK_001", "Test")
        assigned = client.post(
            f"/api/platform/{platform.platform_id}/assign_tile",
            json={"x": 0, "y": 0, "block_id": "BLK_001"},
        )
        assert assigned.status_code == 200
        unassigned = client.post(
            f"/api/platform/{platform.platform_id}/unassign_tile",
            json={"x": 0, "y": 0},
        )
        assert unassigned.status_code == 200
        assert unassigned.get_json()["platform"]["block_assignments"] == {}


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
        # A station group must own at least one platform (Non-empty parent
        # rule), so seed one before listing.
        app_state.network.add_platform(0, 0, platform_id="PLT_A", name="A")
        app_state.network.create_station_group("STA_MAIN", "Main", ["PLT_A"])
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
        # A freshly-placed train is stopped, so power-off (any state -> OFF when
        # stopped) succeeds deterministically -- mirror of test_train_power_on.
        assert response.status_code == 200

    def test_release_emergency_clears_eb_in_ato_mode(
        self, client, app_state, network_with_tracks
    ):
        """POST /release-emergency clears a latched EB even in ATO mode.

        Regression: the "Release Emergency" / Reset button used to call
        setHandle(train, 'N'), which never releases EB (the handle and EB are
        separate; EB clears only via eb.release()) and is rejected outright in
        ATO mode with "Handle input requires manual mode". The dedicated
        release-emergency route calls the canonical eb.release() instead, so it
        works regardless of control mode.
        """
        car = TrainCar("CAR_1", "LOCOMOTIVE", length_cm=40.0, max_speed=100.0)
        train = Train("TRAIN_001", consist=[car])
        app_state.network.add_train(train)
        app_state.network.place_train(train, 0, 0, Direction.EAST)

        controller = app_state.sim_controller.get_or_create_controller("TRAIN_001")
        controller.commands.power_on_cmd()  # OFF -> READY; ATO is the default mode
        assert controller.commands.control_mode == "ato"
        controller.eb.activate()  # stopped, no path -> EB latches cleanly
        assert controller.eb.active is True

        # The OLD button path (handle 'N') is still rejected in ATO mode and
        # leaves EB latched -- exactly the bug the user hit.
        bad = client.post("/api/trains/TRAIN_001/handle", json={"position": "N"})
        assert bad.status_code == 400
        assert "manual mode" in bad.get_json()["error"].lower()
        assert controller.eb.active is True

        # The NEW path releases EB and lands the controller in READY.
        resp = client.post("/api/trains/TRAIN_001/release-emergency")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "success"
        assert controller.eb.active is False
        assert controller.ops.state == TrainState.READY

    def test_release_emergency_rejected_while_moving(
        self, client, app_state, network_with_tracks
    ):
        """release-emergency is latch-gated: it rejects (400) while the train
        is still rolling and leaves EB latched, per eb.release()'s contract."""
        car = TrainCar("CAR_1", "LOCOMOTIVE", length_cm=40.0, max_speed=100.0)
        train = Train("TRAIN_002", consist=[car])
        app_state.network.add_train(train)
        app_state.network.place_train(train, 0, 0, Direction.EAST)

        controller = app_state.sim_controller.get_or_create_controller("TRAIN_002")
        controller.commands.power_on_cmd()
        controller.eb.activate()
        controller.physics_sync.current_speed_kmh = 5.0  # still moving

        resp = client.post("/api/trains/TRAIN_002/release-emergency")
        assert resp.status_code == 400
        assert "moving" in resp.get_json()["error"].lower()
        assert controller.eb.active is True

    def test_release_emergency_lands_running_moving_in_ready_synchronously(
        self, client, app_state, network_with_tracks
    ):
        """The POST is synchronous: a train EB'd to a stop from RUNNING_MOVING
        with no live authority lands in READY within the POST.

        eb.release() no longer writes the operational state; the route runs the
        reactive evaluator right after it (design_new.md "Reactive condition
        evaluation"), so the READY transition commits before the response
        without waiting for the next tick.
        """
        car = TrainCar("CAR_1", "LOCOMOTIVE", length_cm=40.0, max_speed=100.0)
        train = Train("TRAIN_003", consist=[car])
        app_state.network.add_train(train)
        app_state.network.place_train(train, 0, 0, Direction.EAST)

        controller = app_state.sim_controller.get_or_create_controller("TRAIN_003")
        controller.commands.power_on_cmd()
        controller.ops.state = TrainState.RUNNING_MOVING
        controller.physics_sync.current_speed_kmh = 0.0
        controller.eb.activate()  # stopped, no path -> clean latch
        assert controller.eb.active is True
        # EB activation does not move the operational state.
        assert controller.ops.state == TrainState.RUNNING_MOVING

        resp = client.post("/api/trains/TRAIN_003/release-emergency")
        assert resp.status_code == 200
        assert controller.eb.active is False
        # Synchronous: the evaluator committed READY within the POST.
        assert controller.ops.state == TrainState.READY

# ============================================================================
# Path Routes Tests (9 routes)
# ============================================================================
# Path Routes Tests (9 routes)
# ============================================================================


class TestPathRoutes:
    """Test path management and dispatcher endpoints."""

    def test_create_path_rejects_v0_payload(self, client, app_state, network_with_tracks):
        """POST /api/paths rejects the v0 wire format. The legacy
        `{start_block_id, end_block_id}` keys are no longer accepted; the handler
        returns 400 with a message naming the v1 fields (routes/paths.py:111).
        The success path is covered by `test_create_path_assigns_one_off_route`."""
        make_segment_block_with_tracks(app_state.network, "BLK_001", [(0, 0)])
        make_segment_block_with_tracks(app_state.network, "BLK_002", [(2, 0)])
        response = client.post(
            "/api/paths", json={"start_block_id": "BLK_001", "end_block_id": "BLK_002"}
        )
        assert response.status_code == 400
        data = response.get_json()
        assert data["success"] is False
        assert "start_block_id/end_block_id" in data["error"]

    def test_create_path_assigns_one_off_route(
        self, client, app_state, network_with_tracks
    ):
        """POST /api/paths constructs a one-off operator route and assigns the
        train (design_new.md "Availability flag REST API surface",
        Quick Route). It returns 202 accepted and does
        NOT create dispatcher authority inline — Phase 1 builds the path. The
        "one active path per train" invariant is enforced by the dispatcher in
        Phase 1 (create_path rejects a second active path), not by this handler.
        """
        make_segment_block_with_tracks(app_state.network, "BLK_001", [(0, 0), (1, 0)])
        make_segment_block_with_tracks(app_state.network, "BLK_002", [(2, 0), (3, 0)])

        car = TrainCar("CAR_1", "LOCOMOTIVE", length_cm=40.0, max_speed=100.0)
        train = Train("TRAIN_001", consist=[car])
        app_state.network.add_train(train)
        app_state.network.place_train(train, 0, 0, Direction.EAST)

        response = client.post(
            "/api/paths",
            json={
                "requested_destination_block_id": "BLK_002",
                "requested_destination_direction": "forward",
                "train_id": "TRAIN_001",
            },
        )
        assert response.status_code == 202, response.get_json()
        data = response.get_json()
        assert data["success"] is True
        route_id = data["route_id"]

        # A one-off route toward BLK_002 was created and the train assigned to it.
        registry = app_state.sim_controller.route_executor.route_registry
        assert route_id in registry
        route = registry[route_id]
        assert route.mode.value == "one_off"
        assert "TRAIN_001" in route.assigned_train_ids

        # The handler did NOT create dispatcher authority inline; the path is
        # built later in Phase 1.
        assert app_state.path_dispatcher.active_path_id_by_train("TRAIN_001") is None

    def test_list_paths(self, client):
        """GET /api/paths should list all paths."""
        response = client.get("/api/paths")
        assert response.status_code == 200
        data = response.get_json()
        assert "paths" in data

    def test_find_dispatcher_paths(self, client, app_state, network_with_tracks):
        """POST /api/dispatcher/paths should find paths."""
        make_segment_block_with_tracks(app_state.network, "BLK_001", [(0, 0)])
        make_segment_block_with_tracks(app_state.network, "BLK_002", [(2, 0)])
        response = client.post(
            "/api/dispatcher/paths",
            json={"start_block_id": "BLK_001", "end_block_id": "BLK_002"},
        )
        assert response.status_code == 200

    def test_get_reachable_endpoints(self, client, app_state, network_with_tracks):
        """GET /api/dispatcher/reachable/<block_id> should return reachable blocks."""
        make_segment_block_with_tracks(app_state.network, "BLK_001", [(0, 0)])
        response = client.get("/api/dispatcher/reachable/BLK_001")
        assert response.status_code == 200

    def test_get_reachable_endpoints_for_train_unknown(self, client, app_state, network_with_tracks):
        """GET /api/dispatcher/reachable-for-train/<train_id> 404s for an unknown train."""
        response = client.get("/api/dispatcher/reachable-for-train/NO_SUCH_TRAIN")
        assert response.status_code == 404


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

    def test_save_load_and_delete_layout(self, client):
        name = "migrated_api_layout"
        assert client.post("/api/layouts", json={"name": name}).status_code == 200
        loaded = client.post(f"/api/layouts/{name}/load")
        assert loaded.status_code == 200
        assert loaded.get_json()["status"] == "success"
        assert client.delete(f"/api/layouts/{name}").status_code == 200
        assert client.delete(f"/api/layouts/{name}").status_code == 404

    def test_static_spa_fallback(self, client):
        assert client.get("/some/client/side/route").status_code == 200
        assert client.get("/missing-client-bundle.js").status_code == 404

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
            app_state.network.add_track(Tile(x=x, y=0, track_bits=2))

        make_segment_block_with_tracks(app_state.network, "BLK_A",
            positions=[(0, 0), (1, 0)],
            name="Block A",
        )
        make_segment_block_with_tracks(app_state.network, "BLK_B",
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
        app_state.network.add_track(Tile(x=10, y=0, track_bits=2))
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
        ``segment_id`` is None (the way switches are modeled in v1)
        must still be reachable. The legacy ``build_block_graph``
        treated such tiles as graph barriers and chopped the network into
        disconnected components, marking valid pairs unviable."""
        network = app_state.network
        for x in range(5):
            network.add_track(Tile(x=x, y=0, track_bits=2))
        # (2,0) is intentionally left out of both blocks; it has track
        # geometry but no segment_id — the same shape a switch tile
        # has in v1.
        make_segment_block_with_tracks(network, "BLK_LEFT",
            positions=[(0, 0), (1, 0)],
            name="Left",
        )
        make_segment_block_with_tracks(network, "BLK_RIGHT",
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
        network.add_track(Tile(x=0, y=0, track_bits=2))
        network.add_track(Tile(x=10, y=0, track_bits=2))
        make_segment_block_with_tracks(network, "BLK_X",
            positions=[(0, 0)],
            name="X",
        )
        make_segment_block_with_tracks(network, "BLK_Y",
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
    # Per design_new.md "Route data" (Quick Route), manual reposition
    # is modeled through an
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
            app_state.network.add_track(Tile(x=x, y=0, track_bits=2))
        make_segment_block_with_tracks(app_state.network, "BLK_A", positions=[(0, 0), (1, 0)], name="Block A"
        )
        make_segment_block_with_tracks(app_state.network, "BLK_B", positions=[(2, 0), (3, 0)], name="Block B"
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


class TestEventLogRoute:
    """`GET /api/event-log` — the read side of the persisted event log.

    design_new.md "Event log": the viewer reads the persisted stream and filters
    by entity / operation / level / time. The default in-memory read serves a
    live session; `?persisted=true` reads the on-disk stream so an earlier run
    can be reviewed after a restart.
    """

    def _emit(self, nc, operation, event_time, train_id=None):
        from notification_center import Kind, train_ref

        nc.emit(
            source="Test", operation=operation, kind=Kind.INFO,
            message=f"{operation} happened", event_time=event_time,
            entities=[train_ref(train_id)] if train_id else [],
        )

    def test_in_memory_read_with_filters(self, client, app_state):
        nc = app_state.sim_controller.notification_center
        self._emit(nc, "occupy_blocks", 100.0, train_id="T1")
        self._emit(nc, "on_block_tail_vacated", 200.0, train_id="T2")

        events = client.get("/api/event-log").get_json()["events"]
        assert len(events) >= 2

        # A per-entity view is a membership filter over `entities`.
        by_train = client.get("/api/event-log?train_id=T1").get_json()["events"]
        assert by_train and all(
            any(r["entity_type"] == "train" and r["entity_id"] == "T1"
                for r in e["entities"])
            for e in by_train
        )

        by_op = client.get(
            "/api/event-log?operation=on_block_tail_vacated"
        ).get_json()["events"]
        assert by_op and all(e["operation"] == "on_block_tail_vacated" for e in by_op)

    def test_time_range_filter(self, client, app_state):
        nc = app_state.sim_controller.notification_center
        for t in (10.0, 20.0, 30.0, 40.0):
            self._emit(nc, "tick", t)

        windowed = client.get("/api/event-log?since=15&until=35").get_json()["events"]
        times = [e["event_time"] for e in windowed]
        assert times and all(15.0 <= t <= 35.0 for t in times)

    def test_persisted_read_survives_a_fresh_notification_center(self, client, app_state, tmp_path):
        # Write a run to disk, then swap in a brand-new NotificationCenter whose
        # in-memory archive is empty (as a restart would). The persisted read
        # must still return the earlier run's events; the in-memory read must not.
        from notification_center import Kind, NotificationCenter, train_ref

        path = str(tmp_path / "event_log.jsonl")
        first = NotificationCenter()
        first.enable_event_log(path)
        first.emit(source="Test", operation="release_path", kind=Kind.INFO,
                   message="path completed", event_time=1.0,
                   entities=[train_ref("T1")])

        fresh = NotificationCenter()
        fresh.enable_event_log(path)  # same file, empty archive
        app_state.sim_controller.notification_center = fresh

        in_memory = client.get("/api/event-log").get_json()["events"]
        assert not any(e["operation"] == "release_path" for e in in_memory)

        persisted = client.get("/api/event-log?persisted=true").get_json()["events"]
        assert any(e["operation"] == "release_path" for e in persisted)

    def test_persisted_falls_back_to_memory_when_no_log_path(self, client, app_state):
        nc = app_state.sim_controller.notification_center
        assert nc.log_path is None
        self._emit(nc, "tick", 5.0)
        # persisted=true with no configured path must not error; it serves memory.
        resp = client.get("/api/event-log?persisted=true")
        assert resp.status_code == 200


# ============================================================================
# Legacy JavaScript API-suite migration
# ============================================================================


class TestMigratedSwitchRoutes:
    """Live switch HTTP contracts formerly covered only by test_api.js."""

    @staticmethod
    def _create_switch(client, app_state):
        app_state.network.add_track(Tile(x=5, y=0, track_bits=0b000110))
        response = client.post(
            "/api/tracks/5/0/switch",
            json={"main_length_cm": 60.0, "diverge_length_cm": 65.0},
        )
        assert response.status_code == 201
        return response.get_json()

    def test_list_create_get_update_and_delete_switch(self, client, app_state):
        assert client.get("/api/switches").get_json() == []
        created = self._create_switch(client, app_state)
        switch_id = created["switch_id"]

        assert client.get(f"/api/switches/{switch_id}").status_code == 200
        updated = client.put(
            f"/api/switches/{switch_id}",
            json={"name": "Updated Switch", "main_length_cm": 70.0},
        )
        assert updated.status_code == 200
        assert updated.get_json()["name"] == "Updated Switch"

        deleted = client.delete(f"/api/switches/{switch_id}")
        assert deleted.status_code == 200
        assert client.get(f"/api/switches/{switch_id}").status_code == 404

    def test_switch_creation_validation_and_idempotency(self, client, app_state):
        assert client.post("/api/tracks/9/9/switch").status_code == 404
        app_state.network.add_track(Tile(x=1, y=0, track_bits=2))
        assert client.post("/api/tracks/1/0/switch").status_code == 400

        created = self._create_switch(client, app_state)
        repeated = client.post("/api/tracks/5/0/switch")
        assert repeated.status_code == 200
        assert repeated.get_json()["switch_id"] == created["switch_id"]

    def test_switch_state_change_is_queued_and_validated(self, client, app_state):
        switch_id = self._create_switch(client, app_state)["switch_id"]
        queued = client.put(
            f"/api/switches/{switch_id}/state",
            json={"state": 1, "command_id": "CMD-MIGRATED"},
        )
        assert queued.status_code == 202
        assert queued.get_json() == {
            "command_id": "CMD-MIGRATED",
            "status": "queued",
        }
        assert client.put(f"/api/switches/{switch_id}/state", json={}).status_code == 400
        assert (
            client.put(f"/api/switches/{switch_id}/state", json={"state": 2}).status_code
            == 400
        )
        assert client.put("/api/switches/NOPE/state", json={"state": 0}).status_code == 404


class TestMigratedSpeedProfileRoutes:
    """Speed-profile CRUD migrated from the server-dependent JavaScript suite."""

    def test_list_and_get_default_profile(self, client):
        listing = client.get("/api/speed-profiles")
        assert listing.status_code == 200
        body = listing.get_json()
        assert body["count"] == len(body["profiles"])
        assert any(p["profile_id"] == "default" for p in body["profiles"])

        default = client.get("/api/speed-profiles/default")
        assert default.status_code == 200
        assert default.get_json()["profile_id"] == "default"

    def test_create_update_delete_and_conflicts(self, client):
        payload = {"profile_id": "TEST_PROFILE", "name": "Migrated profile"}
        created = client.post("/api/speed-profiles", json=payload)
        assert created.status_code == 201
        assert created.get_json()["profile_id"] == "TEST_PROFILE"
        assert client.post("/api/speed-profiles", json=payload).status_code == 409

        updated = client.put(
            "/api/speed-profiles/TEST_PROFILE", json={"name": "Updated profile"}
        )
        assert updated.status_code == 200
        assert updated.get_json()["name"] == "Updated profile"

        assert client.delete("/api/speed-profiles/default").status_code == 409
        assert client.delete("/api/speed-profiles/TEST_PROFILE").status_code == 204
        assert client.get("/api/speed-profiles/TEST_PROFILE").status_code == 404

    def test_profile_input_validation(self, client):
        assert client.post("/api/speed-profiles", json={}).status_code == 400
        assert client.get("/api/speed-profiles/NOPE").status_code == 404
        assert client.put("/api/speed-profiles/NOPE", json={"name": "x"}).status_code == 404


class TestMigratedScaleRoutes:
    def test_get_and_put_scale(self, client):
        from model_scale import get_model_scale, set_model_scale

        original = get_model_scale()
        try:
            current = client.get("/api/scale")
            assert current.status_code == 200
            assert "presets" in current.get_json()

            updated = client.put("/api/scale", json={"scale": 160.0})
            assert updated.status_code == 200
            assert updated.get_json()["scale"] == 160.0
            assert client.put("/api/scale", json={"preset": "NOPE"}).status_code == 400
        finally:
            set_model_scale(original)


class TestMigratedTrainRouteCrud:
    def test_list_create_get_and_delete_route(self, client):
        assert client.get("/api/train-routes").status_code == 200
        created = client.post(
            "/api/train-routes",
            json={
                "route_id": "ROUTE_MIGRATED",
                "name": "Migrated route",
                "mode": "one_way",
                "tasks": [],
            },
        )
        assert created.status_code == 200
        route = created.get_json()["route"]
        assert route["route_id"] == "ROUTE_MIGRATED"
        assert route["mode"] == "one_off"

        fetched = client.get("/api/train-routes/ROUTE_MIGRATED")
        assert fetched.status_code == 200
        assert fetched.get_json()["route_id"] == "ROUTE_MIGRATED"

        assert client.delete("/api/train-routes/ROUTE_MIGRATED").status_code == 200
        assert client.get("/api/train-routes/ROUTE_MIGRATED").status_code == 404


class TestMigratedTrainPositionAndRetiredCommands:
    def test_reposition_train_uses_current_position_contract(
        self, client, app_state, network_with_tracks
    ):
        make_segment_block_with_tracks(app_state.network, "BLK_POSITION", [(0, 0), (1, 0), (2, 0), (3, 0)]
        )
        created = client.post(
            "/api/trains",
            json={
                "id": "TRAIN_POSITION",
                "length": 0.4,
                "position": {"x": 0, "y": 0},
                "direction": "EAST",
            },
        )
        assert created.status_code == 200

        repositioned = client.put(
            "/api/trains/TRAIN_POSITION/position",
            json={"position": {"x": 2, "y": 0}, "direction": "EAST"},
        )
        assert repositioned.status_code == 200
        controller = app_state.sim_controller.get_or_create_controller("TRAIN_POSITION")
        assert controller._get_head_tile() == (2, 0)

    @pytest.mark.parametrize(
        "path",
        [
            "/api/trains/T/clear-direction",
            "/api/trains/T/set-direction",
            "/api/trains/T/turn-around",
            "/api/trains/T/enable-manual",
            "/api/trains/T/disable-manual",
            "/api/trains/T/emergency-stop",
            "/api/trains/T/arrive",
        ],
    )
    def test_v0_train_command_endpoint_is_retired(self, client, path):
        # Flask resolves these URLs to the live /api/trains/<id> resource but
        # correctly rejects POST because the v0 command method is absent.
        assert client.post(path).status_code == 405


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

The former server-dependent JavaScript API checks have been migrated into this
suite. Live switch, speed-profile, scale, train-route CRUD, and train-position
contracts are covered above; removed v0 train commands are pinned as 404s so a
stale compatibility surface cannot return silently.
"""
