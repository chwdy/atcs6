"""Unit tests for `runtime_payloads.PhaseLatches`.

Covers the four canonical setters (path_cancel_request leaf + the three
composite setters that fan out via the inclusive-latch rule), the
`is_any_pending` predicate, and the four read-and-clear consumers.
"""

import pytest

from runtime_payloads import PendingPathCancelRequest, PhaseLatches


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_default_is_empty():
    latches = PhaseLatches()
    assert latches.path_cancel_request is None
    assert latches.route_abort is False
    assert latches.power_off is False
    assert latches.route_deactivate is False
    assert latches.is_any_pending() is False


# ---------------------------------------------------------------------------
# Leaf writer
# ---------------------------------------------------------------------------


def test_set_path_cancel_request_writes_payload():
    latches = PhaseLatches()
    latches.set_path_cancel_request("eb", "operator EB")
    assert isinstance(latches.path_cancel_request, PendingPathCancelRequest)
    assert latches.path_cancel_request.initiator == "eb"
    assert latches.path_cancel_request.reason == "operator EB"
    assert latches.is_any_pending() is True


def test_set_path_cancel_request_overwrites_most_recent():
    """Per design_new.md: when multiple same-tick writers race, most-recent wins."""
    latches = PhaseLatches()
    latches.set_path_cancel_request("eb", "first")
    latches.set_path_cancel_request("operator_cancelled", "second")
    assert latches.path_cancel_request.initiator == "operator_cancelled"
    assert latches.path_cancel_request.reason == "second"


def test_set_path_cancel_request_reason_optional():
    latches = PhaseLatches()
    latches.set_path_cancel_request("revoked")
    assert latches.path_cancel_request.initiator == "revoked"
    assert latches.path_cancel_request.reason is None


# ---------------------------------------------------------------------------
# Composite setters — bool latch only when no live path
# ---------------------------------------------------------------------------


def test_set_route_abort_without_path_only_sets_bool():
    latches = PhaseLatches()
    latches.set_route_abort("operator stop", has_active_path=False)
    assert latches.route_abort is True
    assert latches.path_cancel_request is None


def test_set_power_off_without_path_only_sets_bool():
    latches = PhaseLatches()
    latches.set_power_off("power off", has_active_path=False)
    assert latches.power_off is True
    assert latches.path_cancel_request is None


def test_set_route_deactivate_without_path_only_sets_bool():
    latches = PhaseLatches()
    latches.set_route_deactivate("route_deactivated:R1", has_active_path=False)
    assert latches.route_deactivate is True
    assert latches.path_cancel_request is None


# ---------------------------------------------------------------------------
# Composite setters — fan-out when live path exists
# ---------------------------------------------------------------------------


def test_set_route_abort_with_path_fans_out_to_inclusive_cancel():
    latches = PhaseLatches()
    latches.set_route_abort("operator stop", has_active_path=True)
    assert latches.route_abort is True
    assert latches.path_cancel_request is not None
    assert latches.path_cancel_request.initiator == "operator_cancelled"
    assert latches.path_cancel_request.reason == "operator stop"


def test_set_power_off_with_path_fans_out_to_inclusive_cancel():
    latches = PhaseLatches()
    latches.set_power_off("op power", has_active_path=True)
    assert latches.power_off is True
    assert latches.path_cancel_request is not None
    assert latches.path_cancel_request.initiator == "power_off"
    assert latches.path_cancel_request.reason == "op power"


def test_set_route_deactivate_with_path_fans_out_to_inclusive_cancel():
    latches = PhaseLatches()
    latches.set_route_deactivate("route_deactivated:R1", has_active_path=True)
    assert latches.route_deactivate is True
    assert latches.path_cancel_request is not None
    assert latches.path_cancel_request.initiator == "revoked"
    assert latches.path_cancel_request.reason == "route_deactivated:R1"


# ---------------------------------------------------------------------------
# is_any_pending
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "setter,kwargs,inactive_field",
    [
        ("set_route_abort", {"has_active_path": False}, "route_abort"),
        ("set_power_off", {"has_active_path": False}, "power_off"),
        ("set_route_deactivate", {"has_active_path": False}, "route_deactivate"),
    ],
)
def test_is_any_pending_for_each_bool_latch(setter, kwargs, inactive_field):
    latches = PhaseLatches()
    assert latches.is_any_pending() is False
    getattr(latches, setter)(**kwargs)
    assert latches.is_any_pending() is True
    assert getattr(latches, inactive_field) is True


def test_is_any_pending_only_for_path_cancel_request():
    latches = PhaseLatches()
    latches.set_path_cancel_request("eb")
    assert latches.is_any_pending() is True
    assert latches.route_abort is False
    assert latches.power_off is False
    assert latches.route_deactivate is False


# ---------------------------------------------------------------------------
# Consumers (read-and-clear)
# ---------------------------------------------------------------------------


def test_consume_path_cancel_request_returns_and_clears():
    latches = PhaseLatches()
    latches.set_path_cancel_request("eb", "operator EB")
    payload = latches.consume_path_cancel_request()
    assert payload is not None
    assert payload.initiator == "eb"
    assert latches.path_cancel_request is None
    # Idempotent on empty.
    assert latches.consume_path_cancel_request() is None


def test_consume_route_abort_returns_and_clears():
    latches = PhaseLatches()
    latches.set_route_abort(has_active_path=False)
    assert latches.consume_route_abort() is True
    assert latches.route_abort is False
    # Idempotent on empty.
    assert latches.consume_route_abort() is False


def test_consume_power_off_returns_and_clears():
    latches = PhaseLatches()
    latches.set_power_off(has_active_path=False)
    assert latches.consume_power_off() is True
    assert latches.power_off is False
    assert latches.consume_power_off() is False


def test_consume_route_deactivate_returns_and_clears():
    latches = PhaseLatches()
    latches.set_route_deactivate(has_active_path=False)
    assert latches.consume_route_deactivate() is True
    assert latches.route_deactivate is False
    assert latches.consume_route_deactivate() is False


def test_consume_does_not_touch_other_latches():
    """Consuming one latch leaves the other three untouched."""
    latches = PhaseLatches()
    latches.set_route_abort(has_active_path=True)
    # route_abort + path_cancel_request both set.
    assert latches.route_abort is True
    assert latches.path_cancel_request is not None

    # Consuming path_cancel_request leaves route_abort intact.
    latches.consume_path_cancel_request()
    assert latches.path_cancel_request is None
    assert latches.route_abort is True

    # Consuming route_abort now clears the last latch.
    latches.consume_route_abort()
    assert latches.route_abort is False
    assert latches.is_any_pending() is False
