"""Smoke tests for `station_registry.StationRegistry`.

End-to-end station/platform CRUD is exercised by `tests/test_stations.py`,
`tests/test_station_block_improvements.py`, and `tests/test_blocks.py`
through the network forwarders. This file just covers the construction
path and lazy-init contract.
"""

from network import RailwayNetwork
from station_registry import StationRegistry


def test_station_registry_constructs_with_network():
    network = RailwayNetwork()
    sr = StationRegistry(network)
    assert sr._n is network


def test_network_lazy_inits_station_registry():
    network = RailwayNetwork()
    assert network._station_registry is None
    sr = network._get_station_registry()
    assert sr is not None
    assert isinstance(sr, StationRegistry)
    assert network._get_station_registry() is sr


def test_tile_to_platform_id_through_forwarder():
    """`network.tile_to_platform_id(...)` lazy-inits and returns None when
    no platform owns the tile."""
    network = RailwayNetwork()
    assert network.tile_to_platform_id(0, 0) is None
    assert network._station_registry is not None  # lazy-init triggered


def test_get_platform_through_forwarder():
    """`network.get_platform(...)` lazy-inits and returns None for unknown ids."""
    network = RailwayNetwork()
    assert network.get_platform("does-not-exist") is None


def test_platforms_dict_is_owned_by_network():
    """`platforms` / `station_groups` / `_tile_to_platform` registries
    stay on RailwayNetwork; StationRegistry reads / writes them via the
    back-reference."""
    network = RailwayNetwork()
    sr = network._get_station_registry()
    # Both views point at the same underlying dict.
    assert sr._n.platforms is network.platforms
    assert sr._n.station_groups is network.station_groups
    assert sr._n._tile_to_platform is network._tile_to_platform
