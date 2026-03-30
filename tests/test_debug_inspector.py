"""
Tests for the Debug Inspector module.

Tests the generic object inspector: path resolution, depth-limited
serialization, and edge cases (circular refs, enums, large collections).
"""

import dataclasses
from collections import deque
from enum import Enum
from typing import Dict, List, Optional

import pytest

from debug_inspector import resolve_path, serialize_object


# ---------------------------------------------------------------------------
# Test helpers — simple mock objects
# ---------------------------------------------------------------------------


class MockState(Enum):
    FREE = "free"
    OCCUPIED = "occupied"


@dataclasses.dataclass
class MockPosition:
    x: int = 0
    y: int = 0
    label: str = "origin"


@dataclasses.dataclass
class MockTrain:
    train_id: str = "T1"
    speed: float = 42.5
    state: MockState = MockState.FREE
    position: Optional[MockPosition] = None
    tags: List[str] = dataclasses.field(default_factory=list)


class MockNetwork:
    """Non-dataclass with __dict__, mimics RailwayNetwork."""

    def __init__(self) -> None:
        self.trains: Dict[str, MockTrain] = {}
        self.blocks: Dict[str, str] = {}
        self.grid: Dict[tuple, str] = {}


class MockRoot:
    """Top-level root mimicking AppState."""

    def __init__(self) -> None:
        self.network = MockNetwork()
        self.running = False


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def root() -> MockRoot:
    r = MockRoot()
    r.network.trains["T1"] = MockTrain(
        train_id="T1",
        speed=60.0,
        state=MockState.OCCUPIED,
        position=MockPosition(3, 5, "yard"),
        tags=["express", "electric"],
    )
    r.network.trains["T2"] = MockTrain(train_id="T2", speed=0.0)
    r.network.blocks = {"B1": "free", "B2": "occupied"}
    r.network.grid = {(0, 0): "straight", (3, 5): "curve"}
    return r


# ---------------------------------------------------------------------------
# resolve_path tests
# ---------------------------------------------------------------------------


class TestResolvePath:
    """Tests for dotted path resolution."""

    def test_empty_path_returns_root(self, root: MockRoot) -> None:
        assert resolve_path(root, "") is root

    def test_single_segment_attribute(self, root: MockRoot) -> None:
        assert resolve_path(root, "network") is root.network

    def test_nested_attribute(self, root: MockRoot) -> None:
        assert resolve_path(root, "network.trains") is root.network.trains

    def test_dict_key_lookup(self, root: MockRoot) -> None:
        result = resolve_path(root, "network.trains.T1")
        assert result.train_id == "T1"

    def test_deep_path(self, root: MockRoot) -> None:
        result = resolve_path(root, "network.trains.T1.position.x")
        assert result == 3

    def test_dataclass_field(self, root: MockRoot) -> None:
        result = resolve_path(root, "network.trains.T1.speed")
        assert result == 60.0

    def test_enum_field(self, root: MockRoot) -> None:
        result = resolve_path(root, "network.trains.T1.state")
        assert result == MockState.OCCUPIED

    def test_list_index(self, root: MockRoot) -> None:
        result = resolve_path(root, "network.trains.T1.tags.0")
        assert result == "express"

    def test_tuple_dict_key(self, root: MockRoot) -> None:
        result = resolve_path(root, "network.grid.3,5")
        assert result == "curve"

    def test_tuple_dict_key_with_spaces(self, root: MockRoot) -> None:
        result = resolve_path(root, "network.grid.0, 0")
        assert result == "straight"

    def test_missing_attribute_raises(self, root: MockRoot) -> None:
        with pytest.raises(KeyError, match="not found"):
            resolve_path(root, "network.nonexistent")

    def test_missing_dict_key_raises(self, root: MockRoot) -> None:
        with pytest.raises(KeyError, match="not found"):
            resolve_path(root, "network.trains.T99")

    def test_invalid_list_index_raises(self, root: MockRoot) -> None:
        with pytest.raises(KeyError, match="not valid"):
            resolve_path(root, "network.trains.T1.tags.99")

    def test_primitive_attribute(self, root: MockRoot) -> None:
        assert resolve_path(root, "running") is False


# ---------------------------------------------------------------------------
# serialize_object tests
# ---------------------------------------------------------------------------


class TestSerializeObject:
    """Tests for depth-limited serialization."""

    def test_none(self) -> None:
        assert serialize_object(None) is None

    def test_int(self) -> None:
        assert serialize_object(42) == 42

    def test_float(self) -> None:
        assert serialize_object(3.14) == 3.14

    def test_bool(self) -> None:
        assert serialize_object(True) is True

    def test_string(self) -> None:
        assert serialize_object("hello") == "hello"

    def test_long_string_truncated(self) -> None:
        long = "x" * 300
        result = serialize_object(long)
        assert result.endswith("...")
        assert len(result) < 300

    def test_enum(self) -> None:
        result = serialize_object(MockState.FREE)
        assert result["_type"] == "enum"
        assert result["value"] == "free"
        assert result["class"] == "MockState"

    def test_dict_depth_0(self) -> None:
        d = {"a": 1, "b": 2}
        result = serialize_object(d, depth=0)
        assert result["_type"] == "dict"
        assert result["length"] == 2
        assert "keys" in result
        assert "items" not in result

    def test_dict_depth_1(self) -> None:
        d = {"a": 1, "b": "hello"}
        result = serialize_object(d, depth=1)
        assert result["_type"] == "dict"
        assert result["items"]["a"] == 1
        assert result["items"]["b"] == "hello"

    def test_list_depth_0(self) -> None:
        result = serialize_object([1, 2, 3], depth=0)
        assert result["_type"] == "list"
        assert result["length"] == 3

    def test_list_depth_1(self) -> None:
        result = serialize_object([10, 20], depth=1)
        assert result["items"] == [10, 20]

    def test_tuple(self) -> None:
        result = serialize_object((1, 2), depth=1)
        assert result["_type"] == "tuple"
        assert result["items"] == [1, 2]

    def test_set(self) -> None:
        result = serialize_object({3, 1, 2}, depth=1)
        assert result["_type"] == "set"
        assert result["length"] == 3

    def test_frozenset(self) -> None:
        result = serialize_object(frozenset([1]), depth=1)
        assert result["_type"] == "frozenset"

    def test_deque(self) -> None:
        d = deque([1, 2, 3], maxlen=10)
        result = serialize_object(d, depth=1)
        assert result["_type"] == "deque"
        assert result["maxlen"] == 10
        assert result["items"] == [1, 2, 3]

    def test_dataclass_depth_0(self) -> None:
        pos = MockPosition(3, 5, "yard")
        result = serialize_object(pos, depth=0)
        assert result["_type"] == "object"
        assert result["class"] == "MockPosition"
        assert isinstance(result["fields"], list)
        assert "x" in result["fields"]

    def test_dataclass_depth_1(self) -> None:
        pos = MockPosition(3, 5, "yard")
        result = serialize_object(pos, depth=1)
        assert result["_type"] == "object"
        assert result["fields"]["x"] == 3
        assert result["fields"]["y"] == 5
        assert result["fields"]["label"] == "yard"

    def test_generic_object_depth_0(self) -> None:
        net = MockNetwork()
        result = serialize_object(net, depth=0)
        assert result["_type"] == "object"
        assert result["class"] == "MockNetwork"
        assert isinstance(result["fields"], list)

    def test_generic_object_depth_1(self) -> None:
        net = MockNetwork()
        net.trains["T1"] = MockTrain()
        result = serialize_object(net, depth=1)
        assert result["fields"]["trains"]["_type"] == "dict"

    def test_nested_dataclass(self) -> None:
        train = MockTrain(
            position=MockPosition(1, 2, "main"),
            state=MockState.OCCUPIED,
        )
        result = serialize_object(train, depth=2)
        pos = result["fields"]["position"]
        assert pos["_type"] == "object"
        assert pos["fields"]["x"] == 1
        assert result["fields"]["state"]["_type"] == "enum"

    def test_circular_reference(self) -> None:
        a: dict = {}
        a["self"] = a
        result = serialize_object(a, depth=2)
        assert result["items"]["self"]["_type"] == "circular"

    def test_truncation_large_dict(self) -> None:
        big = {str(i): i for i in range(600)}
        result = serialize_object(big, depth=1, max_items=10)
        assert len(result["items"]) == 10
        assert result["_truncated"] is True

    def test_truncation_large_list(self) -> None:
        big = list(range(600))
        result = serialize_object(big, depth=1, max_items=10)
        assert len(result["items"]) == 10
        assert result["_truncated"] is True

    def test_callable(self) -> None:
        def my_func():
            pass

        result = serialize_object(my_func)
        assert result["_type"] == "method"
        assert result["name"] == "my_func"

    def test_class_type(self) -> None:
        result = serialize_object(MockTrain)
        assert result["_type"] == "type"
        assert result["name"] == "MockTrain"


# ---------------------------------------------------------------------------
# Integration: resolve + serialize
# ---------------------------------------------------------------------------


class TestInspectorIntegration:
    """End-to-end tests combining resolve_path and serialize_object."""

    def test_inspect_root(self, root: MockRoot) -> None:
        obj = resolve_path(root, "")
        result = serialize_object(obj, depth=1)
        assert result["_type"] == "object"
        assert "network" in result["fields"]

    def test_inspect_train(self, root: MockRoot) -> None:
        obj = resolve_path(root, "network.trains.T1")
        result = serialize_object(obj, depth=1)
        assert result["class"] == "MockTrain"
        assert result["fields"]["speed"] == 60.0
        assert result["fields"]["state"]["value"] == "occupied"

    def test_inspect_position(self, root: MockRoot) -> None:
        obj = resolve_path(root, "network.trains.T1.position")
        result = serialize_object(obj, depth=1)
        assert result["fields"]["x"] == 3
        assert result["fields"]["label"] == "yard"

    def test_inspect_grid_tuple_key(self, root: MockRoot) -> None:
        obj = resolve_path(root, "network.grid.3,5")
        result = serialize_object(obj)
        assert result == "curve"

    def test_depth_0_shows_keys_only(self, root: MockRoot) -> None:
        obj = resolve_path(root, "network.trains")
        result = serialize_object(obj, depth=0)
        assert result["_type"] == "dict"
        assert "T1" in result["keys"]
        assert "T2" in result["keys"]
        assert "items" not in result
