"""Segment deletion keeps its block connected.

design_new.md, **Tile** > **Non-empty parent rule** > "Segment deletion keeps
its block connected": `SegmentGeometryManager.delete_segment(...)` rejects the
deletion of a `Segment` that is a non-terminal member of a multi-segment
`Block`, and changes nothing. A segment may be deleted only when it is a
terminal member of its block (at either end of `Block.segments`) or the block's
only member, so the remaining members stay one connected switch-free run.

The same rule reaches every edit that empties a segment, because an emptied
segment is deleted in the same operation ("Auto-delete on last-child removal")
and a rejected parent delete rejects the whole edit ("Atomic commit"):

    - taking a segment's last tile out of it (`remove_tile_from_segment`,
      `remove_tiles_from_segment`, the segment tile-removal endpoint)
    - erasing track (`remove_track`, the track and bulk-delete endpoints)
    - turning a tile into a switch (a switch is never a segment member)
    - building or extending a platform over a tile (the tile leaves its segment)
    - taking a tile out of a platform

An edit that empties several segments of one block together is judged as one
edit: it is rejected only when the members left behind would not form one
connected run.

Run with:
    /opt/homebrew/anaconda3/envs/general/bin/python -m pytest \
        tests/test_segment_delete_keeps_block_connected.py -v
"""

import threading
from datetime import datetime

import pytest

from app_factory import create_app
from app_state import AppState
from network import RailwayNetwork
from path_dispatcher import PathDispatcher
from persistence import PersistenceManager
from services import NotFoundError, ValidationError, platform_service, segment_service
from services import track_service
from simulation import SimulationController
from track import Tile


def _grouped_line(middle_tiles: int = 1) -> RailwayNetwork:
    """A straight EW run of three segments A - B - C grouped into block G.

    A holds two tiles, B holds `middle_tiles` tiles, C holds two tiles.
    B is the middle member of G, so it is the segment the rule protects.
    """
    net = RailwayNetwork()
    for x in range(2 + middle_tiles + 2):
        net.add_track(Tile(x=x, y=0, track_bits=2))  # EW straight
    net.create_segment_with_tracks("A", [(0, 0), (1, 0)], name="A")
    net.create_segment_with_tracks(
        "B", [(2 + i, 0) for i in range(middle_tiles)], name="B"
    )
    net.create_segment_with_tracks(
        "C", [(2 + middle_tiles + i, 0) for i in range(2)], name="C"
    )
    net._get_block_grouping_manager().group_into_block("G", ["A", "B", "C"])
    return net


def _snapshot(net: RailwayNetwork):
    """Everything a rejected edit must leave exactly as it found it."""
    return (
        {bid: list(block.segment_ids) for bid, block in net.blocks.items()},
        {
            pos: (tile.segment_id, tile.track_bits, tuple(tile.signal_ids))
            for pos, tile in net.tracks.items()
        },
    )


@pytest.fixture
def net() -> RailwayNetwork:
    return _grouped_line()


def _app_state(network: RailwayNetwork) -> AppState:
    path_dispatcher = PathDispatcher(network)
    sim_controller = SimulationController(network, path_dispatcher)
    return AppState(
        network=network,
        path_dispatcher=path_dispatcher,
        sim_controller=sim_controller,
        persistence_manager=PersistenceManager(storage_dir="layouts"),
        simulation_lock=threading.Lock(),
        simulation_running=False,
        simulation_speed=1.0,
        last_update_time=datetime.now(),
    )


@pytest.fixture
def app_state(net) -> AppState:
    return _app_state(net)


@pytest.fixture
def client(app_state):
    app = create_app(app_state)
    app.config["TESTING"] = True
    with app.test_client() as test_client:
        yield test_client


# --------------------------------------------------------------------------- #
# delete_segment
# --------------------------------------------------------------------------- #


class TestDeleteSegment:
    def test_middle_segment_is_rejected_and_changes_nothing(self, net):
        before = _snapshot(net)

        with pytest.raises(ValueError, match="middle of block G"):
            net.delete_segment("B")

        assert _snapshot(net) == before

    def test_the_message_names_the_segment_and_the_way_out(self, net):
        with pytest.raises(ValueError) as excinfo:
            net.delete_segment("B")

        message = str(excinfo.value)
        assert "B" in message and "G" in message
        assert "end" in message  # only an end segment can be deleted

    def test_terminal_segments_can_be_deleted_and_the_block_stays_connected(self, net):
        assert net.delete_segment("A") is True
        assert net.blocks["G"].segment_ids == ["B", "C"]
        assert net.tracks[(0, 0)].segment_id is None

        assert net.delete_segment("C") is True
        assert net.blocks["G"].segment_ids == ["B"]

    def test_the_last_remaining_member_deletes_the_block(self, net):
        net.delete_segment("A")
        net.delete_segment("C")

        # B is now the block's only member, so deleting it is allowed again.
        assert net.delete_segment("B") is True
        assert "G" not in net.blocks

    def test_a_middle_segment_becomes_deletable_once_it_is_an_end(self, net):
        with pytest.raises(ValueError):
            net.delete_segment("B")

        net.delete_segment("A")  # B is now the first member of G

        assert net.delete_segment("B") is True
        assert net.blocks["G"].segment_ids == ["C"]

    def test_unknown_segment_returns_false(self, net):
        before = _snapshot(net)

        assert net.delete_segment("NOPE") is False

        assert _snapshot(net) == before

    def test_a_segment_that_is_its_own_block_deletes_freely(self):
        net = RailwayNetwork()
        for x in range(2):
            net.add_track(Tile(x=x, y=0, track_bits=2))
        net.create_segment_with_tracks("S", [(0, 0), (1, 0)], name="S")

        assert net.delete_segment("S") is True
        assert net.blocks == {}


# --------------------------------------------------------------------------- #
# The check that every emptying edit runs first
# --------------------------------------------------------------------------- #


class TestCheckTilesRemovable:
    def test_emptying_the_middle_segment_is_refused(self, net):
        with pytest.raises(ValueError, match="middle of block G"):
            net.check_tiles_removable([(2, 0)])

    def test_emptying_an_end_segment_is_allowed(self, net):
        net.check_tiles_removable([(0, 0), (1, 0)])

    def test_leaving_the_segment_a_tile_is_allowed(self):
        net = _grouped_line(middle_tiles=2)
        net.check_tiles_removable([(2, 0)])  # B keeps (3, 0)

    def test_tiles_that_belong_to_no_segment_are_ignored(self, net):
        net.check_tiles_removable([(99, 99)])
        net.check_tiles_removable([])

    def test_several_segments_are_judged_as_one_edit(self, net):
        # Emptying A and B together leaves C alone: one connected run.
        net.check_tiles_removable([(0, 0), (1, 0), (2, 0)])
        # Emptying A and C leaves B alone: also one connected run.
        net.check_tiles_removable([(0, 0), (1, 0), (3, 0), (4, 0)])
        # Emptying all three leaves nothing behind.
        net.check_tiles_removable([(x, 0) for x in range(5)])

    def test_checking_changes_nothing(self, net):
        before = _snapshot(net)
        with pytest.raises(ValueError):
            net.check_tiles_removable([(2, 0)])
        assert _snapshot(net) == before


# --------------------------------------------------------------------------- #
# Tile removal and erasing track: rejected as a whole
# --------------------------------------------------------------------------- #


class TestTileRemovalIsAtomic:
    def test_removing_the_middle_segments_last_tile_is_rejected(self, net):
        before = _snapshot(net)

        with pytest.raises(ValueError, match="middle of block G"):
            net.remove_tile_from_segment(2, 0)

        assert _snapshot(net) == before
        assert net.tracks[(2, 0)].segment_id == "B"

    def test_batch_removal_that_empties_the_middle_segment_is_rejected(self, net):
        before = _snapshot(net)

        with pytest.raises(ValueError):
            net.remove_tiles_from_segment([(0, 0), (2, 0)])

        # Nothing was detached, not even the tile of the end segment A.
        assert _snapshot(net) == before

    def test_removing_one_of_two_tiles_keeps_the_middle_segment(self):
        net = _grouped_line(middle_tiles=2)

        net.remove_tile_from_segment(2, 0)

        assert net.blocks["G"].segment_ids == ["A", "B", "C"]
        assert net.tracks[(3, 0)].segment_id == "B"
        assert net.tracks[(2, 0)].segment_id is None

    def test_emptying_an_end_segment_deletes_it_and_keeps_the_block(self, net):
        net.remove_tiles_from_segment([(0, 0), (1, 0)])

        assert net.blocks["G"].segment_ids == ["B", "C"]

    def test_erasing_the_middle_tile_is_rejected_and_the_track_stays(self, net):
        before = _snapshot(net)

        with pytest.raises(ValueError, match="middle of block G"):
            net.remove_track(2, 0)

        assert (2, 0) in net.tracks
        assert _snapshot(net) == before

    def test_erasing_an_end_segments_tiles_is_allowed(self, net):
        assert net.remove_track(0, 0) is True
        assert net.remove_track(1, 0) is True

        assert net.blocks["G"].segment_ids == ["B", "C"]

    def test_erasing_a_whole_block_together_is_allowed(self, net):
        everything = [(x, 0) for x in range(5)]

        for x, y in everything:
            assert net.remove_track(x, y, also_removing=everything) is True

        assert net.tracks == {}
        assert "G" not in net.blocks

    def test_erasing_the_middle_together_with_an_end_is_allowed(self, net):
        together = [(0, 0), (1, 0), (2, 0)]  # A and B; C is left

        for x, y in together:
            net.remove_track(x, y, also_removing=together)

        assert net.blocks["G"].segment_ids == ["C"]

    def test_erasing_only_the_middle_is_still_rejected_alongside_other_tiles(self, net):
        # The extra tile empties nothing, so B is still cut out of the middle.
        with pytest.raises(ValueError):
            net.remove_track(2, 0, also_removing=[(2, 0), (0, 0)])


# --------------------------------------------------------------------------- #
# Services and HTTP
# --------------------------------------------------------------------------- #


class TestSegmentEndpoints:
    def test_delete_middle_segment_returns_400_and_changes_nothing(
        self, client, net
    ):
        before = _snapshot(net)

        response = client.delete("/api/segments/B")

        assert response.status_code == 400
        assert "middle of block G" in response.get_json()["error"]
        assert _snapshot(net) == before

    def test_delete_end_segment_succeeds(self, client, net):
        response = client.delete("/api/segments/A")

        assert response.status_code == 200
        assert net.blocks["G"].segment_ids == ["B", "C"]

    def test_delete_unknown_segment_is_still_404(self, client):
        assert client.delete("/api/segments/NOPE").status_code == 404

    def test_service_raises_a_validation_error(self, app_state, net):
        before = _snapshot(net)

        with pytest.raises(ValidationError, match="middle of block G"):
            segment_service.delete_segment(app_state, "B")

        assert _snapshot(net) == before

    def test_service_still_raises_not_found_for_an_unknown_segment(self, app_state):
        with pytest.raises(NotFoundError):
            segment_service.delete_segment(app_state, "NOPE")

    def test_removing_the_last_tile_of_the_middle_segment_is_rejected(
        self, client, net
    ):
        before = _snapshot(net)

        response = client.delete(
            "/api/segments/B/tracks", json={"positions": [[2, 0]]}
        )

        assert response.status_code == 400
        assert "middle of block G" in response.get_json()["error"]
        # The tile went back into the segment: the edit was rejected as a whole.
        assert _snapshot(net) == before
        # ...and the segment's tile lookup agrees, not just the tile's own field.
        assert net.get_segment_positions("B") == [(2, 0)]

    def test_removing_one_of_two_tiles_from_the_middle_segment_succeeds(self):
        net = _grouped_line(middle_tiles=2)
        app = create_app(_app_state(net))
        app.config["TESTING"] = True
        with app.test_client() as client:
            response = client.delete(
                "/api/segments/B/tracks", json={"positions": [[2, 0]]}
            )

        assert response.status_code == 200
        assert net.blocks["G"].segment_ids == ["A", "B", "C"]
        assert response.get_json()["segment_deleted"] is False


class TestTrackEndpoints:
    def test_deleting_the_middle_tile_returns_400_and_keeps_the_track(
        self, client, net
    ):
        before = _snapshot(net)

        response = client.delete("/api/track/2/0")

        assert response.status_code == 400
        assert "middle of block G" in response.get_json()["error"]
        assert _snapshot(net) == before

    def test_bulk_delete_of_the_middle_tile_is_rejected_before_anything_goes(
        self, client, net
    ):
        before = _snapshot(net)

        response = client.post(
            "/api/tracks/bulk-delete",
            json={"positions": [[0, 0], [2, 0]], "force": True},
        )

        assert response.status_code == 400
        # Not even the tile of the end segment A was erased.
        assert _snapshot(net) == before

    def test_bulk_delete_of_a_whole_block_succeeds(self, client, net):
        everything = [[x, 0] for x in range(5)]

        response = client.post(
            "/api/tracks/bulk-delete",
            json={"positions": everything, "force": True},
        )

        assert response.status_code == 200
        assert net.tracks == {}
        assert "G" not in net.blocks

    def test_switch_bit_edit_on_the_middle_tile_is_rejected(self, app_state, net):
        before = _snapshot(net)

        with pytest.raises(ValidationError, match="middle of block G"):
            track_service.update_track(
                app_state, x=2, y=0, data={"track_bits": 6}  # EW | NE -> switch
            )

        # The tile keeps its old bits: the edit did not half-apply.
        assert _snapshot(net) == before

    def test_switch_bit_edit_on_an_end_tile_is_allowed(self, app_state, net):
        # A holds two tiles, so turning one into a switch only shrinks A.
        result = track_service.update_track(
            app_state, x=0, y=0, data={"track_bits": 6}
        )

        assert result["status"] == "updated"
        assert net.blocks["G"].segment_ids == ["A", "B", "C"]

    def test_bulk_add_that_switches_the_middle_tile_is_rejected(
        self, app_state, net
    ):
        before = _snapshot(net)

        with pytest.raises(ValidationError, match="middle of block G"):
            track_service.add_tracks_bulk(
                app_state, tracks_data=[{"x": 2, "y": 0, "track_bits": 4}]
            )

        assert _snapshot(net) == before

    def test_bulk_delete_service_rejects_before_touching_anything(
        self, app_state, net
    ):
        before = _snapshot(net)

        with pytest.raises(ValidationError):
            track_service.delete_tracks_bulk(
                app_state, positions=[[0, 0], [2, 0]], force=True
            )

        assert _snapshot(net) == before


# --------------------------------------------------------------------------- #
# Platforms: a tile leaves its segment when a platform is built over it
# --------------------------------------------------------------------------- #


def _grouped_line_with_platform_segment() -> RailwayNetwork:
    """PLT_SEG(0) - X(1) - Y(2, 3), with PLT_SEG and X and Y grouped as G.

    The platform's own segment is the first member; X (a single tile) is the
    middle member. A tile outside every segment is where the platform starts.
    """
    net = RailwayNetwork()
    for x in range(4):
        net.add_track(Tile(x=x, y=0, track_bits=2))
    net.create_segment_with_tracks("X", [(1, 0)], name="X")
    net.create_segment_with_tracks("Y", [(2, 0), (3, 0)], name="Y")
    net.add_platform(0, 0, platform_id="PLT", name="P")
    platform_segment = net.tracks[(0, 0)].segment_id
    net._get_block_grouping_manager().group_into_block(
        "G", [platform_segment, "X", "Y"]
    )
    return net


class TestPlatforms:
    def test_building_a_platform_on_the_middle_segments_last_tile_is_rejected(
        self, net
    ):
        before = _snapshot(net)

        with pytest.raises(ValueError, match="middle of block G"):
            net.add_platform(2, 0, platform_id="PLT", name="P")

        assert "PLT" not in net.platforms
        assert _snapshot(net) == before

    def test_extending_a_platform_over_the_middle_segments_last_tile_is_rejected(
        self,
    ):
        net = _grouped_line_with_platform_segment()
        before = _snapshot(net)

        with pytest.raises(ValueError, match="middle of block G"):
            net.extend_platform("PLT", [(1, 0)])

        assert net.platforms["PLT"].positions == [(0, 0)]
        assert _snapshot(net) == before

    def test_the_platform_service_reports_it_as_a_validation_error(self):
        app_state = _app_state(_grouped_line_with_platform_segment())

        with pytest.raises(ValidationError, match="middle of block G"):
            platform_service.extend_platform(app_state, "PLT", positions=[[1, 0]])

    def test_taking_the_last_tile_out_of_a_middle_platform_is_rejected(self):
        net = RailwayNetwork()
        for x in range(5):
            net.add_track(Tile(x=x, y=0, track_bits=2))
        net.create_segment_with_tracks("A", [(0, 0), (1, 0)], name="A")
        net.create_segment_with_tracks("C", [(3, 0), (4, 0)], name="C")
        net.add_platform(2, 0, platform_id="PLT", name="P")
        platform_segment = net.tracks[(2, 0)].segment_id
        net._get_block_grouping_manager().group_into_block(
            "G", ["A", platform_segment, "C"]
        )
        app_state = _app_state(net)
        before = _snapshot(net)

        with pytest.raises(ValidationError, match="middle of block G"):
            platform_service.remove_tile(app_state, "PLT", x=2, y=0)

        assert "PLT" in net.platforms
        assert net.platforms["PLT"].positions == [(2, 0)]
        assert _snapshot(net) == before
