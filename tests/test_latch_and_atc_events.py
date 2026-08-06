"""Deferred actions and ATC reason changes are events.

design_new.md "Error events" (Deferred actions are events too): a latch records
an action the runtime means to take and cannot commit yet, so setting one and
committing one are both reported. Without the set event the intent is invisible
whenever a second writer overwrites the payload before Phase 3 reaches it —
most-recent-payload-wins means the first writer's `ErrorRecord` would otherwise
never be seen at all.

design_new.md "canonical lookahead payload" (A changed reason code is an event):
the distance-0 `atc_limit_reason_code` is recomputed every tick and stored
nowhere, so a train that held at zero for a minute leaves no trace of why unless
the change itself is reported.
"""

import pytest

from runtime_payloads import ErrorRecord, PhaseLatches


class RecordingNotifier:
    """Stands in for `controller.notifications.emit`, keeping the same keyword
    contract so the aggregate is exercised exactly as it is wired."""

    def __init__(self):
        self.events = []
        # The controller reaches its sink as `notifications.emit(...)`, so the
        # recorder answers to that name as well as being callable directly.
        self.emit = self

    def __call__(self, message, *, operation, kind, error=None, payload=None):
        self.events.append(
            {
                "message": message,
                "operation": operation,
                "kind": kind,
                "error": error,
                "payload": payload or {},
            }
        )

    def operations(self):
        return [e["operation"] for e in self.events]


@pytest.fixture
def latches():
    notifier = RecordingNotifier()
    lat = PhaseLatches()
    lat.notify = notifier
    return lat, notifier


# ---------------------------------------------------------------------------
# Latch writes


def test_setting_the_leaf_latch_reports_it(latches):
    lat, notifier = latches
    lat.set_path_cancel_request("eb", "ATC overspeed escalation")

    assert notifier.operations() == ["set_path_cancel_request"]
    ev = notifier.events[0]
    assert ev["kind"] == "info"
    assert ev["payload"]["latch"] == "path_cancel_request"
    assert ev["payload"]["initiator"] == "eb"


def test_a_write_carrying_an_error_record_is_a_warning(latches):
    lat, notifier = latches
    record = ErrorRecord("head is not in the resource sequence", "out_of_sequence")
    lat.set_path_cancel_request("deviation", deviation=True, error=record)

    ev = notifier.events[0]
    assert ev["kind"] == "warning"
    assert ev["error"] is record
    assert ev["payload"]["deviation"] is True


def test_an_overwritten_intent_is_still_reported(latches):
    """The reason this rule exists.

    Two writers in the same rolling window overwrite one payload slot, so the
    first `ErrorRecord` never reaches Phase 3. It must still be in the stream.
    """
    lat, notifier = latches
    first = ErrorRecord("deviated off the authority", "deviation")
    second = ErrorRecord("operator pulled the plug", "power_off")
    lat.set_path_cancel_request("deviation", error=first)
    lat.set_path_cancel_request("power_off", error=second)

    # The latch itself kept only the most recent payload...
    assert lat.path_cancel_request.error is second
    # ...but both intents are on the stream.
    assert [e["error"] for e in notifier.events] == [first, second]


def test_an_inclusive_teardown_reports_both_halves(latches):
    """A fan-out emits one event per helper, so the intent and the cancellation
    it implies each read back rather than one hiding the other."""
    lat, notifier = latches
    lat.set_route_abort("operator abort", has_active_path=True)

    assert notifier.operations() == ["set_route_abort", "set_path_cancel_request"]
    assert lat.route_abort is True
    assert lat.path_cancel_request.initiator == "operator_cancelled"


def test_a_teardown_with_no_live_path_reports_only_itself(latches):
    lat, notifier = latches
    lat.set_power_off("power off", has_active_path=False)

    assert notifier.operations() == ["set_power_off"]
    assert lat.path_cancel_request is None


# ---------------------------------------------------------------------------
# Latch commits


def test_committing_a_latch_reports_it(latches):
    lat, notifier = latches
    lat.set_route_deactivate("route deactivated", has_active_path=False)
    notifier.events.clear()

    assert lat.consume_route_deactivate() is True
    assert notifier.operations() == ["consume_route_deactivate"]
    assert notifier.events[0]["kind"] == "info"


def test_an_empty_consume_reports_nothing(latches):
    """Phase 3 reaches all four consumers every tick for every train. Emitting
    on the empty pass would bury the stream in events saying nothing happened."""
    lat, notifier = latches

    assert lat.consume_path_cancel_request() is None
    assert lat.consume_route_abort() is False
    assert lat.consume_power_off() is False
    assert lat.consume_route_deactivate() is False
    assert notifier.events == []


def test_the_commit_carries_the_committed_payload(latches):
    lat, notifier = latches
    lat.set_path_cancel_request("run_complete", route_progress_kind="run_complete")
    notifier.events.clear()

    lat.consume_path_cancel_request()
    payload = notifier.events[0]["payload"]
    assert payload["initiator"] == "run_complete"
    assert payload["run_complete"] is True


def test_an_aggregate_with_no_notifier_is_silent():
    """An isolated unit-test aggregate has no sink wired; a latch write must
    still work, because reporting never changes what the latch did."""
    lat = PhaseLatches()
    lat.set_route_abort("abort", has_active_path=True)
    assert lat.route_abort is True
    assert lat.path_cancel_request is not None


def test_the_notifier_is_wiring_not_latch_state():
    """Two aggregates holding the same latches are the same latches, whichever
    sink they report to — otherwise equality would depend on plumbing, and the
    generated payload types would carry a callable."""
    import dataclasses

    a = PhaseLatches()
    a.notify = RecordingNotifier()
    b = PhaseLatches()
    assert a == b
    b.route_abort = True
    assert a != b
    assert "notify" not in {f.name for f in dataclasses.fields(PhaseLatches)}


def test_a_reporting_failure_never_reaches_the_caller():
    def explode(*_args, **_kwargs):
        raise RuntimeError("sink is down")

    lat = PhaseLatches()
    lat.notify = explode
    lat.set_power_off("power off", has_active_path=False)
    assert lat.power_off is True


# ---------------------------------------------------------------------------
# ATC reason-code changes


class FakeAtc:
    def __init__(self):
        self._last_limit_reason_code = None
        self.speed_limit = 0.0


class ReasonCodeHarness:
    """The reporter reads only `self.atc` and `self.notifications`, so it is
    exercised directly rather than through a whole controller and network."""

    def __init__(self):
        from train_controller import TrainController

        self.atc = FakeAtc()
        self.notifications = RecordingNotifier()
        self._last_emitted_atc_reason_code = None
        #: Stands in for the canonical distance-0 detail property.
        self.speed_limit_reason = None
        self._report = TrainController._report_atc_reason_change.__get__(self)

    def emit_for(self, code, detail=None):
        self.atc._last_limit_reason_code = code
        self.speed_limit_reason = detail
        self._report()


def test_a_changed_reason_code_is_reported():
    h = ReasonCodeHarness()
    h.emit_for("caution")

    assert h.notifications.operations() == ["tick"]
    ev = h.notifications.events[0]
    assert ev["payload"]["reason_code"] == "caution"
    assert ev["payload"]["previous_reason_code"] is None


def test_a_steady_reason_code_is_silent():
    """Emit on change, not per tick — the code is recomputed every tick and a
    train holding for a minute must not produce a minute of identical events."""
    h = ReasonCodeHarness()
    h.emit_for("caution")
    h.notifications.events.clear()

    for _ in range(20):
        h.emit_for("caution")
    assert h.notifications.events == []


def test_a_flickering_code_reports_each_real_change():
    """Compared against the last code emitted, not last tick's, so an
    alternating pair does not collapse into silence."""
    h = ReasonCodeHarness()
    h.emit_for("caution")
    h.emit_for("authority_stop")
    h.emit_for("caution")

    codes = [e["payload"]["reason_code"] for e in h.notifications.events]
    assert codes == ["caution", "authority_stop", "caution"]


@pytest.mark.parametrize(
    "code,expected",
    [
        ("deviation", "warning"),
        ("no_authority", "warning"),
        ("caution", "debug"),
        ("authority_stop", "debug"),
        ("speed_limit", "debug"),
    ],
)
def test_only_the_codes_that_hold_the_train_are_warnings(code, expected):
    """The rest are ordinary driving restrictions a developer reads back rather
    than faults, so they must not colour the stream as problems."""
    h = ReasonCodeHarness()
    h.emit_for(code)
    assert h.notifications.events[0]["kind"] == expected


def test_an_enum_code_is_reported_by_its_value():
    from enums import AtcLimitReasonCode

    h = ReasonCodeHarness()
    h.emit_for(AtcLimitReasonCode.NO_AUTHORITY)
    assert h.notifications.events[0]["payload"]["reason_code"] == "no_authority"


def test_clearing_to_no_code_reports_nothing_but_rearms():
    """A controller that computes no code has nothing to say, but the next real
    code is news again rather than being swallowed as unchanged."""
    h = ReasonCodeHarness()
    h.emit_for("caution")
    h.notifications.events.clear()

    h.emit_for(None)
    assert h.notifications.events == []

    h.emit_for("caution")
    assert [e["payload"]["reason_code"] for e in h.notifications.events] == ["caution"]


def test_the_detail_rides_along_when_the_lookahead_has_one():
    h = ReasonCodeHarness()
    h.emit_for("obstruction", detail="BLK_7 occupied by TRAIN_2")
    assert (
        h.notifications.events[0]["payload"]["atc_limit_reason_detail"]
        == "BLK_7 occupied by TRAIN_2"
    )
