"""Unit tests for `route_target_resolver.RouteTargetResolver`.

Pure helpers (`resolve_task_direction`, `compute_next_task_index`) are
exercised directly. End-to-end `find_next_task_target` behaviour is
covered by `tests/test_route_execution.py` through the
`RouteExecutor._find_next_task_target` forwarder.
"""

from dataclasses import dataclass

from route_target_resolver import RouteTargetResolver
from train_route import RouteMode


# ---------------------------------------------------------------------------
# resolve_task_direction (static, pure)
# ---------------------------------------------------------------------------


def test_resolve_task_direction_passes_through_explicit_forward():
    assert RouteTargetResolver.resolve_task_direction(
        train=None, block_id="BLK_X", task_direction="forward", signed_direction=1
    ) == "forward"


def test_resolve_task_direction_passes_through_explicit_reverse():
    assert RouteTargetResolver.resolve_task_direction(
        train=None, block_id="BLK_X", task_direction="reverse", signed_direction=1
    ) == "reverse"


def test_resolve_task_direction_either_signed_positive_is_forward():
    assert RouteTargetResolver.resolve_task_direction(
        train=None, block_id="BLK_X", task_direction="either", signed_direction=1
    ) == "forward"


def test_resolve_task_direction_either_signed_zero_is_forward():
    assert RouteTargetResolver.resolve_task_direction(
        train=None, block_id="BLK_X", task_direction="either", signed_direction=0
    ) == "forward"


def test_resolve_task_direction_either_signed_negative_is_reverse():
    assert RouteTargetResolver.resolve_task_direction(
        train=None, block_id="BLK_X", task_direction="either", signed_direction=-1
    ) == "reverse"


# ---------------------------------------------------------------------------
# compute_next_task_index (static, pure)
# ---------------------------------------------------------------------------


@dataclass
class _Route:
    mode: RouteMode


@dataclass
class _ExecState:
    current_waypoint_index: int = 0


def test_compute_next_task_index_zero_count_returns_none():
    res = RouteTargetResolver.compute_next_task_index(
        _Route(RouteMode.ONE_OFF), _ExecState(0), task_count=0
    )
    assert res is None


def test_compute_next_task_index_in_range_returns_idx():
    res = RouteTargetResolver.compute_next_task_index(
        _Route(RouteMode.ONE_OFF), _ExecState(2), task_count=5
    )
    assert res == 2


def test_compute_next_task_index_one_off_past_end_returns_none():
    res = RouteTargetResolver.compute_next_task_index(
        _Route(RouteMode.ONE_OFF), _ExecState(7), task_count=5
    )
    assert res is None


def test_compute_next_task_index_circular_wraps():
    res = RouteTargetResolver.compute_next_task_index(
        _Route(RouteMode.CIRCULAR), _ExecState(7), task_count=5
    )
    assert res == 2  # 7 % 5 == 2


def test_compute_next_task_index_round_trip_past_end_reflects():
    """ROUND_TRIP past the end → task_count - 2 (second-to-last)."""
    res = RouteTargetResolver.compute_next_task_index(
        _Route(RouteMode.ROUND_TRIP), _ExecState(5), task_count=5
    )
    assert res == 3


def test_compute_next_task_index_round_trip_negative_reflects():
    """ROUND_TRIP below 0 → 1 (second tile)."""
    res = RouteTargetResolver.compute_next_task_index(
        _Route(RouteMode.ROUND_TRIP), _ExecState(-1), task_count=5
    )
    assert res == 1


def test_compute_next_task_index_round_trip_single_task():
    """Single-task ROUND_TRIP edge case — overflow reflects to 0."""
    res = RouteTargetResolver.compute_next_task_index(
        _Route(RouteMode.ROUND_TRIP), _ExecState(2), task_count=1
    )
    assert res == 0
