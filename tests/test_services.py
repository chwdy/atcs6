"""Service-layer unit tests.

These exercise the service functions directly, without going through
HTTP. They are the architectural win of the refactor: the same code path
the Flask handlers use is now callable from any plain Python context
(CLI tools, journey tests, scripted scenarios) and raises typed
exceptions instead of returning Flask responses.

Run with: ``conda run -n general python -m pytest tests/test_services.py -q``
"""

import threading

import pytest

from app_state import AppState
from network import RailwayNetwork
from persistence import PersistenceManager
from simulation import SimulationController
from track import Tile

from entity_ids import block_id_for_segment, segment_id_for_block
from services import ConflictError, GoneError, NotFoundError, ValidationError
from tests.segment_test_helpers import make_segment_block
from services import (
    block_service,
    platform_service,
    segment_service,
    switch_service,
    track_service,
)


@pytest.fixture
def state():
    """Fresh AppState with no HTTP layer attached."""
    network = RailwayNetwork()
    sim_controller = SimulationController(network)
    persistence_manager = PersistenceManager(storage_dir="layouts")

    return AppState(
        network=network,
        sim_controller=sim_controller,
        persistence_manager=persistence_manager,
        simulation_lock=threading.Lock(),
    )



# ---------------------------------------------------- train_route_service ----



# ------------------------------------------------- speed_profile_service ----



# -------------------------------------------------------- switch_service ----


@pytest.fixture
def state_with_switch_track(state):
    """AppState with one switch-track tile at (5, 0).

    track_bits = EW | NE (0b000110) makes the tile a switch: it has both a
    long path (EW) and a short path (NE), so switch_points is non-empty and
    `is_switch` is True -- the same shape demo_3 uses for a switch tile.
    """
    state.network.add_track(Tile(x=5, y=0, track_bits=0b000110))
    return state


class TestSwitchService:
    def test_create_switch_at_track_is_idempotent(self, state_with_switch_track):
        """First call creates `SW_5_0`; a second returns the same switch."""
        state = state_with_switch_track
        switch = switch_service.create_switch_at_track(state, 5, 0, data={})
        assert switch.switch_id == "SW_5_0"
        assert state.network.get_switch("SW_5_0") is switch

        again = switch_service.create_switch_at_track(state, 5, 0, data={})
        assert again is switch
        assert len(state.network.switches) == 1

    def test_create_switch_no_track_raises_not_found(self, state):
        with pytest.raises(NotFoundError):
            switch_service.create_switch_at_track(state, 9, 9, data={})

    def test_create_switch_non_switch_track_raises_validation(self, state):
        """A straight (EW-only) tile is a track but not a switch."""
        state.network.add_track(Tile(x=1, y=0, track_bits=2))
        with pytest.raises(ValidationError):
            switch_service.create_switch_at_track(state, 1, 0, data={})

    def test_update_switch_forwards_config_fields(self, state_with_switch_track):
        """Non-availability fields forward to RailwayNetwork.update_switch."""
        state = state_with_switch_track
        switch_service.create_switch_at_track(state, 5, 0, data={})
        updated = switch_service.update_switch(
            state, "SW_5_0", data={"name": "Renamed", "main_length_cm": 12.5}
        )
        assert updated.name == "Renamed"
        assert updated.main_length_cm == 12.5


    def test_disable_free_switch_succeeds(self, state_with_switch_track):
        """A free switch accepts the availability flags."""
        state = state_with_switch_track
        switch_service.create_switch_at_track(state, 5, 0, data={})
        updated = switch_service.update_switch(
            state, "SW_5_0", data={"disabled": True, "manual": True}
        )
        assert updated.disabled is True
        assert updated.manual is True

    def test_delete_switch_missing_raises_not_found(self, state):
        with pytest.raises(NotFoundError):
            switch_service.delete_switch(state, "NOPE")

    def test_request_position_change_returns_command_id(self, state_with_switch_track):
        """A valid request echoes the caller's command id (outcome is async)."""
        state = state_with_switch_track
        switch_service.create_switch_at_track(state, 5, 0, data={})
        command_id = switch_service.request_position_change(
            state, "SW_5_0", new_state=1, command_id="CMD-1"
        )
        assert command_id == "CMD-1"

    def test_request_position_change_invalid_state_raises_validation(
        self, state_with_switch_track
    ):
        state = state_with_switch_track
        switch_service.create_switch_at_track(state, 5, 0, data={})
        with pytest.raises(ValidationError):
            switch_service.request_position_change(state, "SW_5_0", new_state=2)

    def test_request_position_change_unknown_switch_raises_not_found(self, state):
        with pytest.raises(NotFoundError):
            switch_service.request_position_change(state, "NOPE", new_state=0)


# --------------------------------------------------------- train_service ----



# --------------------------------------------------------- block_service ----


def _straight_ew(state, xs):
    """Add a straight EW run of tracks at ``xs`` (all joined)."""
    from track import TRACK_EW

    for x in xs:
        state.network.add_track(Tile(x, 0, TRACK_EW))


class TestBlockService:
    """Grouping + config surface. Geometry lives on segments (see below)."""


    def test_create_block_groups_segments(self, state):
        _straight_ew(state, range(4))
        state.network.create_segment_with_tracks("SEG_A", [(0, 0), (1, 0)])
        state.network.create_segment_with_tracks("SEG_B", [(2, 0), (3, 0)])
        block = block_service.create_block(
            state, block_id="BLK_1", segment_ids=["SEG_A", "SEG_B"], name="Yard"
        )
        assert block.block_id == "BLK_1"
        assert block.segment_ids == ["SEG_A", "SEG_B"]
        # Grouping removes the members' prior single-segment blocks.
        assert "SEG_A" not in state.network.blocks
        assert "SEG_B" not in state.network.blocks

    def test_create_block_autogenerates_id(self, state):
        _straight_ew(state, [0, 1])
        state.network.create_segment_with_tracks("SEG_ONLY", [(0, 0), (1, 0)])
        block = block_service.create_block(
            state, segment_ids=["SEG_ONLY"], name="auto"
        )
        assert block.block_id.startswith("BLK_")

    def test_create_block_empty_segment_ids_raises(self, state):
        with pytest.raises(ValidationError):
            block_service.create_block(state, block_id="BLK_E", segment_ids=[])

    def test_create_block_unknown_segment_raises(self, state):
        with pytest.raises(ValidationError):
            block_service.create_block(
                state, block_id="BLK_U2", segment_ids=["UNIDENTIFIED"]
            )

    def test_update_block_name(self, state):
        make_segment_block(state.network, "BLK_U", "old")
        updated = block_service.update_block(state, "BLK_U", data={"name": "new"})
        assert updated.name == "new"

    def test_update_block_unknown_raises_not_found(self, state):
        with pytest.raises(NotFoundError):
            block_service.update_block(state, "UNIDENTIFIED", data={"name": "x"})

    def test_update_block_state_field_raises_gone(self, state):
        """The removed direct `state` mutation surfaces as GoneError (410)."""
        make_segment_block(state.network, "BLK_G")
        with pytest.raises(GoneError):
            block_service.update_block(state, "BLK_G", data={"state": "OCCUPIED"})

    def test_update_block_rejects_geometry_field(self, state):
        """A block config PATCH carrying geometry is rejected (edit the
        segment)."""
        make_segment_block(state.network, "BLK_GEO")
        with pytest.raises(ValidationError):
            block_service.update_block(state, "BLK_GEO", data={"length_cm": 50.0})

    def test_disable_occupied_block_raises_conflict(self, state):
        block = make_segment_block(state.network, "BLK_O")
        block.occupied_by = "TRAIN_A"
        with pytest.raises(ConflictError):
            block_service.update_block(state, "BLK_O", data={"disabled": True})
        assert block.disabled is False  # rejected, not applied

    def test_disable_free_block_succeeds(self, state):
        make_segment_block(state.network, "BLK_F")
        updated = block_service.update_block(
            state, "BLK_F", data={"disabled": True, "manual": True}
        )
        assert updated.disabled is True
        assert updated.manual is True

    def test_allowed_travel_directions_invalid_raises_validation(self, state):
        make_segment_block(state.network, "BLK_D")
        with pytest.raises(ValidationError):
            block_service.update_block(
                state, "BLK_D", data={"allowed_travel_directions": ["sideways"]}
            )

    def test_allowed_travel_directions_valid(self, state):
        make_segment_block(state.network, "BLK_D2")
        updated = block_service.update_block(
            state, "BLK_D2", data={"allowed_travel_directions": ["forward"]}
        )
        assert updated.allowed_travel_directions == {"forward"}

    def test_delete_block_ungroups_to_own_blocks(self, state):
        """Dissolving a grouping re-homes each member into its own block; the
        geometry survives."""
        _straight_ew(state, range(4))
        state.network.create_segment_with_tracks("SEG_A", [(0, 0), (1, 0)])
        state.network.create_segment_with_tracks("SEG_B", [(2, 0), (3, 0)])
        block_service.create_block(
            state, block_id="BLK_1", segment_ids=["SEG_A", "SEG_B"]
        )

        assert block_service.delete_block(state, "BLK_1") is True

        assert block_service.get_block(state, "BLK_1") is None
        # Each freed segment is back in its own single-segment block; the tiles
        # were never deleted.
        assert state.network.get_segment("SEG_A") is not None
        assert state.network.get_segment("SEG_B") is not None
        # A re-homed segment lands in its own block, whose id is the segment's
        # derived onto the block axis — never the same string.
        assert block_id_for_segment("SEG_A") in state.network.blocks
        assert block_id_for_segment("SEG_B") in state.network.blocks
        assert state.network.get_track(0, 0).segment_id == "SEG_A"

    def test_delete_block_unknown_raises_not_found(self, state):
        with pytest.raises(NotFoundError):
            block_service.delete_block(state, "UNIDENTIFIED")

    def test_add_and_remove_member_segment(self, state):
        _straight_ew(state, range(4))
        state.network.create_segment_with_tracks("SEG_A", [(0, 0), (1, 0)])
        state.network.create_segment_with_tracks("SEG_B", [(2, 0), (3, 0)])
        block_service.create_block(state, block_id="BLK_1", segment_ids=["SEG_A"])

        block = block_service.add_segment_to_block(state, "BLK_1", "SEG_B")
        assert block.segment_ids == ["SEG_A", "SEG_B"]

        assert block_service.remove_segment_from_block(state, "BLK_1", "SEG_B") is True
        assert state.network.get_block("BLK_1").segment_ids == ["SEG_A"]
        # Re-homed to its own block, under the derived block-axis id.
        assert block_id_for_segment("SEG_B") in state.network.blocks

    def test_add_segment_unknown_block_raises_not_found(self, state):
        _straight_ew(state, [0, 1])
        state.network.create_segment_with_tracks("SEG_A", [(0, 0), (1, 0)])
        with pytest.raises(NotFoundError):
            block_service.add_segment_to_block(state, "UNIDENTIFIED", "SEG_A")


# ------------------------------------------------------- segment_service ----


class TestSegmentService:
    """Geometry surface: create-with-tracks, geometry PATCH, tiles, delete."""

    def test_create_segment_with_tracks(self, state):
        _straight_ew(state, range(3))
        block = segment_service.create_segment_with_tracks(
            state, segment_id="SEG_T", positions=[[0, 0], [1, 0], [2, 0]], name="T"
        )
        assert block.segment_ids == ["SEG_T"]
        assert state.network.get_segment("SEG_T") is not None

    def test_update_segment_geometry(self, state):
        _straight_ew(state, [0, 1])
        segment_service.create_segment_with_tracks(
            state, segment_id="SEG_G", positions=[[0, 0], [1, 0]]
        )
        seg = segment_service.update_segment(
            state,
            "SEG_G",
            data={"length_cm": 120.0, "min_radius_cm": 30.0, "grade_percent": 2.0},
        )
        assert seg.length_cm == 120.0
        assert seg.min_radius_cm == 30.0
        assert seg.grade_percent == 2.0

    def test_update_segment_unknown_raises_not_found(self, state):
        with pytest.raises(NotFoundError):
            segment_service.update_segment(state, "UNIDENTIFIED", data={"length_cm": 1.0})

    def test_delete_segment_cascades_to_owning_platform(self, state):
        # A station/platform owning exactly one block: deleting the block's only
        # segment empties the block, which must drop it from the platform's
        # block_assignments and, since that empties the platform, delete the
        # platform too (design_new.md "Platform and station model").
        from track import TRACK_NS

        state.network.add_track(Tile(0, 0, TRACK_NS))
        platform = state.network.add_station(
            0, 0, station_id="STA_CASCADE", name="Cascade", length_cm=200.0
        )
        block_id = f"{platform.platform_id}_BLOCK"
        assert block_id in state.network.blocks
        assert platform.platform_id in state.network.platforms
        assert block_id in platform.get_all_assigned_blocks()

        # Deleting is on the geometry axis, and a block id never names a
        # segment, so the block's one member is addressed by its derived id.
        assert (
            segment_service.delete_segment(state, segment_id_for_block(block_id))
            is True
        )

        assert block_id not in state.network.blocks
        # The platform held only this block, so the cascade removed it too.
        assert platform.platform_id not in state.network.platforms

    def test_remove_last_tile_cascades_to_owning_platform(self, state):
        # Removing the LAST tile of a platform-owned block empties its segment;
        # the auto-delete must run the same downward platform cascade, so the
        # emptied platform is torn down instead of left with a dangling
        # block_assignments entry (design_new.md "Platform and station model").
        from track import TRACK_NS

        state.network.add_track(Tile(0, 0, TRACK_NS))
        platform = state.network.add_station(
            0, 0, station_id="STA_RMTILE", name="RmTile", length_cm=200.0
        )
        block_id = f"{platform.platform_id}_BLOCK"
        assert block_id in state.network.blocks
        assert platform.platform_id in state.network.platforms

        # Remove the segment's only tile -> segment empties -> block auto-deleted.
        segment_service.remove_tracks_from_segment(
            state, segment_id_for_block(block_id), positions=[[0, 0]]
        )

        assert block_id not in state.network.blocks
        # The auto-delete cascaded through the service layer: the now-empty
        # platform is gone, not left dangling.
        assert platform.platform_id not in state.network.platforms

    def test_delete_segment_unknown_raises_not_found(self, state):
        with pytest.raises(NotFoundError):
            segment_service.delete_segment(state, "UNIDENTIFIED")

    def test_add_tracks_unknown_segment_raises_not_found(self, state):
        with pytest.raises(NotFoundError):
            segment_service.add_tracks_to_segment(state, "UNIDENTIFIED", positions=[[0, 0]])

    def test_remove_tracks_unknown_segment_raises_not_found(self, state):
        with pytest.raises(NotFoundError):
            segment_service.remove_tracks_from_segment(
                state, "UNIDENTIFIED", positions=[[0, 0]]
            )

    def test_can_extend_unknown_segment_raises_not_found(self, state):
        with pytest.raises(NotFoundError):
            segment_service.can_extend_segment(state, "UNIDENTIFIED", x=0, y=0)


# --------------------------------------------------------- track_service ----


class TestTrackService:

    def test_update_missing_raises_not_found(self, state):
        with pytest.raises(NotFoundError):
            track_service.update_track(state, 9, 9, data={"track_bits": 2})

    def test_update_invalid_body_raises_validation(self, state):
        state.network.add_track(Tile(x=0, y=0, track_bits=2))
        with pytest.raises(ValidationError):
            track_service.update_track(state, 0, 0, data=["not", "a", "dict"])

    def test_update_track_bits_updates(self, state):
        state.network.add_track(Tile(x=0, y=0, track_bits=2))
        result = track_service.update_track(state, 0, 0, data={"track_bits": 1})
        assert result["status"] == "updated"
        assert result["track"].track_bits == 1

    def test_delete_missing_raises_not_found(self, state):
        with pytest.raises(NotFoundError):
            track_service.delete_track(state, 9, 9)

    def test_delete_track(self, state):
        state.network.add_track(Tile(x=0, y=0, track_bits=2))
        assert track_service.delete_track(state, 0, 0) is True
        assert track_service.get_track(state, 0, 0) is None

    def test_add_tracks_bulk_creates(self, state):
        result = track_service.add_tracks_bulk(
            state,
            tracks_data=[
                {"x": 0, "y": 0, "track_bits": 2},
                {"x": 1, "y": 0, "track_bits": 2},
            ],
        )
        assert len(result["added"]) == 2
        assert len(result["updated"]) == 0
        assert track_service.get_track(state, 0, 0) is not None

    def test_add_tracks_bulk_updates_existing(self, state):
        state.network.add_track(Tile(x=0, y=0, track_bits=2))
        result = track_service.add_tracks_bulk(
            state, tracks_data=[{"x": 0, "y": 0, "track_bits": 2}]
        )
        assert len(result["updated"]) == 1
        assert len(result["added"]) == 0

    def test_delete_tracks_bulk_no_impact(self, state):
        state.network.add_track(Tile(x=0, y=0, track_bits=2))
        state.network.add_track(Tile(x=1, y=0, track_bits=2))
        result = track_service.delete_tracks_bulk(state, positions=[[0, 0], [1, 0]])
        assert result["status"] == "done"
        assert result["deleted"] == [[0, 0], [1, 0]]
        assert track_service.get_track(state, 0, 0) is None



# ------------------------------------------------------ platform_service ----


class TestPlatformService:
    def test_get_and_require_platform(self, state):
        assert platform_service.get_platform(state, "UNIDENTIFIED") is None
        with pytest.raises(NotFoundError):
            platform_service.require_platform(state, "UNIDENTIFIED")

    def test_create_platform(self, state):
        state.network.add_track(Tile(x=0, y=0, track_bits=2))
        platform = platform_service.create_platform(
            state, x=0, y=0, platform_id="PLT_1", name="P1"
        )
        assert platform.platform_id == "PLT_1"
        assert state.network.get_platform("PLT_1") is platform

    def test_update_platform(self, state):
        state.network.add_track(Tile(x=0, y=0, track_bits=2))
        platform_service.create_platform(state, x=0, y=0, platform_id="PLT_U", name="old")
        updated = platform_service.update_platform(
            state, "PLT_U", data={"name": "new", "disabled": True}
        )
        assert updated.name == "new"
        assert updated.disabled is True

    def test_update_unknown_raises_not_found(self, state):
        with pytest.raises(NotFoundError):
            platform_service.update_platform(state, "UNIDENTIFIED", data={"name": "x"})

    def test_update_bad_platform_side_raises_validation_error(self, state):
        # Regression: an invalid platform_side must surface as ValidationError
        # (-> HTTP 400) rather than a bare ValueError leaking as HTTP 500, so
        # update_platform matches the create/extend bad-input contract.
        state.network.add_track(Tile(x=0, y=0, track_bits=2))
        platform_service.create_platform(state, x=0, y=0, platform_id="PLT_S")
        with pytest.raises(ValidationError):
            platform_service.update_platform(
                state, "PLT_S", data={"platform_side": "sideways"}
            )

    def test_delete_platform(self, state):
        state.network.add_track(Tile(x=0, y=0, track_bits=2))
        platform_service.create_platform(state, x=0, y=0, platform_id="PLT_X")
        assert platform_service.delete_platform(state, "PLT_X") is True
        assert platform_service.get_platform(state, "PLT_X") is None

    def test_delete_unknown_raises_not_found(self, state):
        with pytest.raises(NotFoundError):
            platform_service.delete_platform(state, "UNIDENTIFIED")

    def test_validate_placement(self, state):
        assert platform_service.validate_placement(state, 9, 9)["can_place"] is False
        state.network.add_track(Tile(x=0, y=0, track_bits=2))
        assert platform_service.validate_placement(state, 0, 0)["can_place"] is True

    def test_can_extend_unknown_platform_raises_not_found(self, state):
        with pytest.raises(NotFoundError):
            platform_service.can_extend(state, "UNIDENTIFIED", x=0, y=0)

    def test_assign_tile_unknown_platform_raises_not_found(self, state):
        with pytest.raises(NotFoundError):
            platform_service.assign_tile(state, "UNIDENTIFIED", x=0, y=0, block_id="BLK")

    def test_assign_tile_unknown_block_raises_not_found(self, state):
        state.network.add_track(Tile(x=0, y=0, track_bits=2))
        platform_service.create_platform(state, x=0, y=0, platform_id="PLT_A")
        with pytest.raises(NotFoundError):
            platform_service.assign_tile(
                state, "PLT_A", x=0, y=0, block_id="UNIDENT_BLK"
            )

    def test_unassign_tile_unknown_platform_raises_not_found(self, state):
        with pytest.raises(NotFoundError):
            platform_service.unassign_tile(state, "UNIDENTIFIED", x=0, y=0)

    def test_remove_tile_unknown_platform_raises_not_found(self, state):
        with pytest.raises(NotFoundError):
            platform_service.remove_tile(state, "UNIDENTIFIED", x=0, y=0)
