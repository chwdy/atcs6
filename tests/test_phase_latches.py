"""Unit tests for `runtime_payloads.PhaseLatches`.

Covers the four canonical setters (path_cancel_request leaf + the three
composite setters that fan out via the inclusive-latch rule), the
`is_any_pending` predicate, and the `clear(...)` field clear each Phase-3
consumer step uses. Per design_new.md "Latch Table" (No shared consume
accessor) this aggregate exposes no `consume_*()` accessor for these four.
"""

import pytest

from runtime_payloads import ErrorRecord, PendingPathCancelRequest, PhaseLatches


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
    latches.set_path_cancel_request(ErrorRecord("operator EB", "eb"), "warning")
    assert isinstance(latches.path_cancel_request, PendingPathCancelRequest)
    assert latches.path_cancel_request.error.origin == "eb"
    assert latches.path_cancel_request.error.text == "operator EB"
    assert latches.path_cancel_request.kind == "warning"
    assert latches.is_any_pending() is True


def test_set_path_cancel_request_overwrites_most_recent():
    """Per design_new.md: when multiple same-tick writers race, most-recent wins."""
    latches = PhaseLatches()
    latches.set_path_cancel_request(ErrorRecord("first", "eb"), "warning")
    latches.set_path_cancel_request(
        ErrorRecord("second", "operator_cancelled"), "info"
    )
    assert latches.path_cancel_request.error.origin == "operator_cancelled"
    assert latches.path_cancel_request.error.text == "second"
    assert latches.path_cancel_request.kind == "info"


def test_kind_is_recorded_independently_of_the_error_record():
    """design_new.md "Writer-helper rule": `kind` is what the writer declared,
    never derived from whether an `ErrorRecord` is present. An ordinary
    operator cancellation is `info` and still carries a diagnostic."""
    latches = PhaseLatches()
    latches.set_path_cancel_request(
        ErrorRecord("operator cancelled their own path", "operator_cancelled"),
        "info",
    )
    assert latches.path_cancel_request.kind == "info"


def test_the_writer_requires_an_error_record():
    """design_new.md "Latch Table", Payload fields: `error` is required on every
    cancel request."""
    latches = PhaseLatches()
    with pytest.raises(ValueError):
        latches.set_path_cancel_request(None, "info")


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
    assert latches.path_cancel_request.error.origin == "operator_cancelled"
    assert latches.path_cancel_request.error.text == "operator stop"
    assert latches.path_cancel_request.kind == "info"


def test_set_power_off_with_path_fans_out_to_inclusive_cancel():
    latches = PhaseLatches()
    latches.set_power_off("op power", has_active_path=True)
    assert latches.power_off is True
    assert latches.path_cancel_request is not None
    assert latches.path_cancel_request.error.origin == "power_off"
    assert latches.path_cancel_request.error.text == "op power"


def test_set_route_deactivate_with_path_fans_out_to_inclusive_cancel():
    latches = PhaseLatches()
    latches.set_route_deactivate("route_deactivated:R1", has_active_path=True)
    assert latches.route_deactivate is True
    assert latches.path_cancel_request is not None
    assert latches.path_cancel_request.error.origin == "revoked"
    assert latches.path_cancel_request.error.text == "route_deactivated:R1"


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
    latches.set_path_cancel_request(ErrorRecord("operator EB", "eb"), "warning")
    assert latches.is_any_pending() is True
    assert latches.route_abort is False
    assert latches.power_off is False
    assert latches.route_deactivate is False


# ---------------------------------------------------------------------------
# Clearing
# ---------------------------------------------------------------------------

# design_new.md "Latch Table" (No shared consume accessor): each field is
# cleared by its own named Phase-3 consumer step through `clear(...)`, and the
# consume-side EVENT is emitted by that step's own consume-side call
# (`cancel_path`, `remove_train_route_run`, `complete_power_off`) — never from
# the aggregate.


@pytest.mark.parametrize(
    "field_name,setter,empty",
    [
        ("route_abort", "set_route_abort", False),
        ("power_off", "set_power_off", False),
        ("route_deactivate", "set_route_deactivate", False),
    ],
)
def test_clear_resets_one_bool_latch(field_name, setter, empty):
    latches = PhaseLatches()
    getattr(latches, setter)(has_active_path=False)
    assert getattr(latches, field_name) is True
    latches.clear(field_name)
    assert getattr(latches, field_name) is empty
    # Idempotent on an already-empty field.
    latches.clear(field_name)
    assert getattr(latches, field_name) is empty


def test_clear_resets_the_payload_latch():
    latches = PhaseLatches()
    latches.set_path_cancel_request(ErrorRecord("operator EB", "eb"), "warning")
    latches.clear("path_cancel_request")
    assert latches.path_cancel_request is None


def test_the_aggregate_exposes_no_consume_accessor():
    """design_new.md "Latch Table": `TrainController.consume_service_stop()` is
    the only `consume_*()` accessor in v1, and it owns the one latch outside
    this aggregate."""
    latches = PhaseLatches()
    assert not [n for n in dir(latches) if n.startswith("consume_")]


def test_clear_rejects_a_name_that_is_not_a_latch_field():
    latches = PhaseLatches()
    with pytest.raises(ValueError):
        latches.clear("service_stop_active")


def test_clearing_one_latch_does_not_touch_the_other_three():
    latches = PhaseLatches()
    latches.set_route_abort(has_active_path=True)
    # route_abort + path_cancel_request both set.
    assert latches.route_abort is True
    assert latches.path_cancel_request is not None

    # Clearing path_cancel_request leaves route_abort intact.
    latches.clear("path_cancel_request")
    assert latches.path_cancel_request is None
    assert latches.route_abort is True

    # Clearing route_abort now empties the last latch.
    latches.clear("route_abort")
    assert latches.route_abort is False
    assert latches.is_any_pending() is False
