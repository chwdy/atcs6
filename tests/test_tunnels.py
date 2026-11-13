"""Tests for the tunnel table on RailwayNetwork.

Covers Phase B of the tunnel feature: the table itself, the writer
helpers, and the auto-cleanup sweep that runs on topology mutation. The
fall-through into ``get_neighbor`` / ``get_connection_edges`` lands in
Phase C and has its own tests.
"""

import pytest

from network import RailwayNetwork
from track import Tile, TRACK_EW, TRACK_NS


def _segs(x, y, bits):
    return Tile(x=x, y=y, track_bits=bits)


def _two_dead_end_tiles() -> RailwayNetwork:
    """Two isolated EW tiles far apart — both have W and E dead-ends."""
    n = RailwayNetwork()
    n.add_track(_segs(0, 0, TRACK_EW))
    n.add_track(_segs(10, 0, TRACK_EW))
    return n


class TestAddTunnel:
    def test_add_creates_symmetric_pair(self):
        n = _two_dead_end_tiles()
        n.add_tunnel((0, 0, "W"), (10, 0, "E"))
        assert n.tunnels[(0, 0, "W")] == (10, 0, "E")
        assert n.tunnels[(10, 0, "E")] == (0, 0, "W")
        assert n.tunnels.get((0, 0, "W")) == (10, 0, "E")
        assert n.tunnels.get((10, 0, "E")) == (0, 0, "W")

    def test_get_tunnel_returns_none_for_non_tunneled(self):
        n = _two_dead_end_tiles()
        assert n.tunnels.get((0, 0, "E")) is None

    def test_edge_normalized_to_upper(self):
        n = _two_dead_end_tiles()
        n.add_tunnel((0, 0, "w"), (10, 0, "e"))
        assert (0, 0, "W") in n.tunnels

    def test_rejects_same_endpoint(self):
        n = _two_dead_end_tiles()
        with pytest.raises(ValueError, match="distinct"):
            n.add_tunnel((0, 0, "W"), (0, 0, "W"))

    def test_rejects_endpoint_without_track(self):
        n = _two_dead_end_tiles()
        with pytest.raises(ValueError, match="No track"):
            n.add_tunnel((0, 0, "W"), (99, 99, "E"))

    def test_rejects_edge_not_in_track(self):
        n = _two_dead_end_tiles()
        # (0, 0) has TRACK_EW — its N edge is not in track.edges.
        with pytest.raises(ValueError, match="not present"):
            n.add_tunnel((0, 0, "N"), (10, 0, "E"))

    def test_rejects_when_endpoint_already_tunneled(self):
        n = _two_dead_end_tiles()
        n.add_track(_segs(20, 0, TRACK_EW))
        n.add_tunnel((0, 0, "W"), (10, 0, "E"))
        with pytest.raises(ValueError, match="already tunneled"):
            n.add_tunnel((0, 0, "W"), (20, 0, "E"))

    def test_rejects_non_dead_end_endpoint(self):
        """An edge whose grid neighbour connects back is not a dead-end."""
        n = RailwayNetwork()
        n.add_track(_segs(0, 0, TRACK_EW))
        n.add_track(_segs(1, 0, TRACK_EW))  # grid-connected to (0,0) via E/W
        n.add_track(_segs(10, 0, TRACK_EW))
        with pytest.raises(ValueError, match="not a dead-end"):
            n.add_tunnel((0, 0, "E"), (10, 0, "W"))

    def test_rejects_invalid_edge_string(self):
        n = _two_dead_end_tiles()
        with pytest.raises(ValueError, match="N/E/S/W"):
            n.add_tunnel((0, 0, "X"), (10, 0, "E"))


class TestRemoveTunnel:
    def test_remove_drops_both_directions(self):
        n = _two_dead_end_tiles()
        n.add_tunnel((0, 0, "W"), (10, 0, "E"))
        assert n.remove_tunnel((0, 0, "W")) is True
        assert n.tunnels == {}

    def test_remove_either_endpoint_works(self):
        n = _two_dead_end_tiles()
        n.add_tunnel((0, 0, "W"), (10, 0, "E"))
        assert n.remove_tunnel((10, 0, "E")) is True
        assert n.tunnels == {}

    def test_remove_returns_false_when_absent(self):
        n = _two_dead_end_tiles()
        assert n.remove_tunnel((0, 0, "W")) is False


class TestSweepStaleTunnels:
    def test_removing_a_tile_drops_its_tunnels(self):
        n = _two_dead_end_tiles()
        n.add_tunnel((0, 0, "W"), (10, 0, "E"))
        n.remove_track(0, 0)
        assert n.tunnels == {}

    def test_filling_the_grid_gap_drops_the_tunnel(self):
        """Once a real grid neighbour appears that connects back, the
        tunnel endpoint is no longer a dead-end → tunnel auto-removes."""
        n = RailwayNetwork()
        n.add_track(_segs(0, 0, TRACK_EW))
        n.add_track(_segs(10, 0, TRACK_EW))
        n.add_tunnel((0, 0, "E"), (10, 0, "W"))
        # Now place a tile right next to (0, 0) that connects back via W:
        n.add_track(_segs(1, 0, TRACK_EW))
        # Tunnel from (0,0,E) is now superseded by real grid geometry.
        assert (0, 0, "E") not in n.tunnels
        assert (10, 0, "W") not in n.tunnels

    def test_unrelated_tile_edit_keeps_tunnel_intact(self):
        n = _two_dead_end_tiles()
        n.add_track(_segs(5, 5, TRACK_NS))  # unrelated tile far away
        n.add_tunnel((0, 0, "W"), (10, 0, "E"))
        # Adding another unrelated tile should not disturb the tunnel.
        n.add_track(_segs(7, 7, TRACK_NS))
        assert n.tunnels[(0, 0, "W")] == (10, 0, "E")


class TestTunnelEdgePairing:
    def test_accepts_non_opposite_edges(self):
        """Tunnels can pair any two dead-end edges, including N↔W and
        other non-cardinal-opposite combinations."""
        n = _two_dead_end_tiles()
        # (20,0) TRACK_NS has N and S as dead-ends.
        n.add_track(_segs(20, 0, TRACK_NS))
        n.add_tunnel((0, 0, "W"), (20, 0, "N"))
        assert n.tunnels[(0, 0, "W")] == (20, 0, "N")
        assert n.tunnels[(20, 0, "N")] == (0, 0, "W")
        # Connectivity flows both ways through a non-opposite pair.
        assert n.get_connection_edges((0, 0), (20, 0)) == ("W", "N")
        assert n.get_connection_edges((20, 0), (0, 0)) == ("N", "W")
        # Both tunneled edges drop out of dead_end_ports.
        assert "W" not in n.get_track(0, 0).dead_end_ports
        assert "N" not in n.get_track(20, 0).dead_end_ports


class TestGetNeighborFollowsTunnels:
    def test_tunneled_dead_end_resolves_to_far_tile(self):
        n = _two_dead_end_tiles()
        n.add_tunnel((0, 0, "W"), (10, 0, "E"))
        far = n.get_neighbor(0, 0, "W")
        assert far is not None
        assert (far.x, far.y) == (10, 0)
        far_back = n.get_neighbor(10, 0, "E")
        assert far_back is not None
        assert (far_back.x, far_back.y) == (0, 0)

    def test_non_tunneled_dead_end_still_returns_none(self):
        n = _two_dead_end_tiles()
        # No tunnel: the E edge of (0,0) is still a dead-end.
        assert n.get_neighbor(0, 0, "E") is None

    def test_grid_neighbor_wins_when_present(self):
        """Real grid geometry overrides any (non-existent) tunnel — adding
        a connecting grid tile uses real adjacency."""
        n = RailwayNetwork()
        n.add_track(_segs(0, 0, TRACK_EW))
        n.add_track(_segs(1, 0, TRACK_EW))  # real grid neighbour
        far = n.get_neighbor(0, 0, "E")
        assert (far.x, far.y) == (1, 0)


class TestGetConnectionEdgesFollowsTunnels:
    def test_tunneled_pair_returns_edge_pair(self):
        n = _two_dead_end_tiles()
        n.add_tunnel((0, 0, "W"), (10, 0, "E"))
        assert n.get_connection_edges((0, 0), (10, 0)) == ("W", "E")
        assert n.get_connection_edges((10, 0), (0, 0)) == ("E", "W")

    def test_non_tunneled_distant_pair_returns_none(self):
        n = _two_dead_end_tiles()
        assert n.get_connection_edges((0, 0), (10, 0)) is None

    def test_grid_adjacent_pair_still_works(self):
        n = RailwayNetwork()
        n.add_track(_segs(0, 0, TRACK_EW))
        n.add_track(_segs(1, 0, TRACK_EW))
        assert n.get_connection_edges((0, 0), (1, 0)) == ("E", "W")


class TestDeadEndPortsUpdatedByTunnel:
    def test_tunneled_edge_is_not_a_dead_end_port(self):
        n = _two_dead_end_tiles()
        # Before the tunnel, (0,0) has W and E as dead-ends.
        assert "W" in n.get_track(0, 0).dead_end_ports
        n.add_tunnel((0, 0, "W"), (10, 0, "E"))
        # After the tunnel, the W edge is no longer dead.
        assert "W" not in n.get_track(0, 0).dead_end_ports
        assert "E" not in n.get_track(10, 0).dead_end_ports
        # The other free edges remain dead.
        assert "E" in n.get_track(0, 0).dead_end_ports
        assert "W" in n.get_track(10, 0).dead_end_ports

    def test_removing_tunnel_restores_dead_end(self):
        n = _two_dead_end_tiles()
        n.add_tunnel((0, 0, "W"), (10, 0, "E"))
        n.remove_tunnel((0, 0, "W"))
        assert "W" in n.get_track(0, 0).dead_end_ports
        assert "E" in n.get_track(10, 0).dead_end_ports


class TestTunnelPersistence:
    def test_layout_dict_round_trip(self):
        n = _two_dead_end_tiles()
        n.add_tunnel((0, 0, "W"), (10, 0, "E"))
        snapshot = n.to_layout_dict()
        assert snapshot["tunnels"] == [
            {
                "a": {"x": 0, "y": 0, "edge": "W"},
                "b": {"x": 10, "y": 0, "edge": "E"},
            }
        ]
        n2 = RailwayNetwork()
        n2.from_dict(snapshot)
        assert n2.tunnels[(0, 0, "W")] == (10, 0, "E")

    def test_to_dict_includes_tunnels(self):
        n = _two_dead_end_tiles()
        n.add_tunnel((0, 0, "W"), (10, 0, "E"))
        payload = n.to_dict()
        assert "tunnels" in payload
        assert len(payload["tunnels"]) == 1

    def test_legacy_snapshot_without_tunnels_loads_clean(self):
        n = _two_dead_end_tiles()
        snapshot = n.to_layout_dict()
        snapshot.pop("tunnels")
        n2 = RailwayNetwork()
        n2.from_dict(snapshot)
        assert n2.tunnels == {}

    def test_malformed_tunnel_entry_is_skipped_not_fatal(self):
        n = _two_dead_end_tiles()
        snapshot = n.to_layout_dict()
        snapshot["tunnels"] = [
            {"a": {"x": 0, "y": 0, "edge": "W"}, "b": {"x": 99, "y": 99, "edge": "E"}},  # bad: no track at (99,99)
        ]
        n2 = RailwayNetwork()
        n2.from_dict(snapshot)
        assert n2.tunnels == {}
