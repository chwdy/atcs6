"""Smoke tests for `block_geometry_manager.BlockGeometryManager`.

Block lifecycle behaviour (create / extend / delete / split) is covered
end-to-end by `tests/test_blocks.py` through the network forwarders. This
file just covers the construction path and lazy-init contract.
"""

from network import RailwayNetwork
from block_geometry_manager import BlockGeometryManager


def test_block_geometry_manager_constructs_with_network():
    network = RailwayNetwork()
    bgm = BlockGeometryManager(network)
    assert bgm._n is network


def test_network_lazy_inits_block_geometry_manager():
    network = RailwayNetwork()
    assert network._block_geometry_manager is None
    bgm = network._get_block_geometry_manager()
    assert bgm is not None
    assert isinstance(bgm, BlockGeometryManager)
    assert network._get_block_geometry_manager() is bgm


def test_create_block_through_forwarder_lazy_inits():
    """`network.create_block(...)` triggers BGM lazy-init and returns the block."""
    network = RailwayNetwork()
    block = network.create_block(block_id="BLK_TEST_1", name="Test Block")
    assert block is not None
    assert block.block_id == "BLK_TEST_1"
    assert "BLK_TEST_1" in network.blocks
    assert network._block_geometry_manager is not None
