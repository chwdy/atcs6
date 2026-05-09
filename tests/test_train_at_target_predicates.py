"""Unit tests for `train_at_target_predicates.TrainAtTargetPredicates`.

The pure helper `get_target_platform_tile` is covered directly here.
End-to-end predicate behaviour (`is_train_at_block`, etc.) is exercised
through the `RouteExecutor` forwarders by `tests/test_route_execution.py`
and friends.
"""

from dataclasses import dataclass
from typing import List, Optional, Tuple

from enums import Direction
from train_at_target_predicates import TrainAtTargetPredicates


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@dataclass
class _StubPlatform:
    positions: List[Tuple[int, int]]


@dataclass
class _StubTrain:
    facing_direction: Optional[Direction] = None


# ---------------------------------------------------------------------------
# get_target_platform_tile (static, pure)
# ---------------------------------------------------------------------------


def test_get_target_platform_tile_empty_returns_none():
    res = TrainAtTargetPredicates.get_target_platform_tile(
        train=_StubTrain(), platform=_StubPlatform(positions=[])
    )
    assert res is None


def test_get_target_platform_tile_single_returns_only_position():
    res = TrainAtTargetPredicates.get_target_platform_tile(
        train=_StubTrain(facing_direction=Direction.NORTH),
        platform=_StubPlatform(positions=[(3, 4)]),
    )
    assert res == (3, 4)


def test_get_target_platform_tile_no_direction_returns_first():
    res = TrainAtTargetPredicates.get_target_platform_tile(
        train=_StubTrain(facing_direction=None),
        platform=_StubPlatform(positions=[(0, 0), (1, 0), (2, 0)]),
    )
    assert res == (0, 0)


def test_get_target_platform_tile_north_picks_highest_y():
    """Travelling NORTH (y increases) → stop at the highest-y tile."""
    res = TrainAtTargetPredicates.get_target_platform_tile(
        train=_StubTrain(facing_direction=Direction.NORTH),
        platform=_StubPlatform(positions=[(0, 0), (0, 5), (0, 2)]),
    )
    assert res == (0, 5)


def test_get_target_platform_tile_south_picks_lowest_y():
    res = TrainAtTargetPredicates.get_target_platform_tile(
        train=_StubTrain(facing_direction=Direction.SOUTH),
        platform=_StubPlatform(positions=[(0, 0), (0, 5), (0, 2)]),
    )
    assert res == (0, 0)


def test_get_target_platform_tile_east_picks_highest_x():
    res = TrainAtTargetPredicates.get_target_platform_tile(
        train=_StubTrain(facing_direction=Direction.EAST),
        platform=_StubPlatform(positions=[(0, 0), (5, 0), (2, 0)]),
    )
    assert res == (5, 0)


def test_get_target_platform_tile_west_picks_lowest_x():
    res = TrainAtTargetPredicates.get_target_platform_tile(
        train=_StubTrain(facing_direction=Direction.WEST),
        platform=_StubPlatform(positions=[(0, 0), (5, 0), (2, 0)]),
    )
    assert res == (0, 0)


def test_get_target_station_tile_aliases_platform_tile():
    """`get_target_station_tile` is a compatibility alias for the platform variant."""
    expected = TrainAtTargetPredicates.get_target_platform_tile(
        train=_StubTrain(facing_direction=Direction.EAST),
        platform=_StubPlatform(positions=[(0, 0), (5, 0)]),
    )
    actual = TrainAtTargetPredicates.get_target_station_tile(
        train=_StubTrain(facing_direction=Direction.EAST),
        station=_StubPlatform(positions=[(0, 0), (5, 0)]),
    )
    assert actual == expected
