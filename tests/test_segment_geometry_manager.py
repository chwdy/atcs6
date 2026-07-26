"""Smoke tests for `segment_geometry_manager.SegmentGeometryManager`.

Block lifecycle behaviour (create / extend / delete / split) is covered
end-to-end by `tests/test_blocks.py` through the network forwarders. This
file just covers the construction path and lazy-init contract.
"""

from network import RailwayNetwork
from segment_geometry_manager import SegmentGeometryManager


def test_segment_geometry_manager_constructs_with_network():
    network = RailwayNetwork()
    bgm = SegmentGeometryManager(network)
    assert bgm._n is network


def test_network_lazy_inits_segment_geometry_manager():
    network = RailwayNetwork()
    assert network._segment_geometry_manager is None
    bgm = network._get_segment_geometry_manager()
    assert bgm is not None
    assert isinstance(bgm, SegmentGeometryManager)
    assert network._get_segment_geometry_manager() is bgm


def test_create_block_through_forwarder_lazy_inits():
    """`network.create_segment(...)` triggers SGM lazy-init and returns the block."""
    network = RailwayNetwork()
    block = network.create_segment(segment_id="BLK_TEST_1", name="Test Block")
    assert block is not None
    assert block.block_id == "BLK_TEST_1"
    assert "BLK_TEST_1" in network.blocks
    assert network._segment_geometry_manager is not None
