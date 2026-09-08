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

    def __call__(
        self, message, *, operation, kind, error=None, payload=None,
        latch_event=False,
    ):
        self.events.append(
            {
                "message": message,
                "operation": operation,
                "kind": kind,
                "error": error,
                "payload": payload or {},
                "latch_event": latch_event,
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
    lat.set_path_cancel_request(
        ErrorRecord("ATC overspeed escalation", "eb"), "warning"
    )

    assert notifier.operations() == ["set_path_cancel_request"]
    ev = notifier.events[0]
    assert ev["kind"] == "warning"
    assert ev["payload"]["latch"] == "path_cancel_request"
    assert ev["payload"]["origin"] == "eb"


def test_every_latch_write_is_marked_a_latch_event(latches):
    """design_new.md "Error events" (Latch events are exempt): a latch write
    passes `latch_event=True`, so the unchanged-repeat suppression never drops
    it even when it carries an `ErrorRecord`. Without that, a second identical
    cancellation would leave the intent invisible."""
    lat, notifier = latches
    lat.set_route_abort("operator abort", has_active_path=True)

    assert [e["latch_event"] for e in notifier.events] == [True, True]


def test_the_writer_declares_the_level_rather_than_deriving_it(latches):
    """design_new.md "Writer-helper rule": `kind` is recorded unchanged and
    independently of the optional `ErrorRecord`. An ordinary operator
    cancellation stays `info` even though it carries a diagnostic, which is
    what keeps it out of `latest_error_for(...)`."""
    lat, notifier = latches
    record = ErrorRecord("operator cancelled their own path", "operator_cancelled")
    lat.set_path_cancel_request(record, "info")

    ev = notifier.events[0]
    assert ev["kind"] == "info"
    assert ev["error"] is record


def test_a_deviation_write_declares_warning(latches):
    lat, notifier = latches
    record = ErrorRecord("head is not in the resource sequence", "out_of_sequence")
    lat.set_path_cancel_request(record, "warning", deviation=True)

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
    first = ErrorRecord("deviated off the authority", "out_of_sequence")
    second = ErrorRecord("operator pulled the plug", "power_off")
    lat.set_path_cancel_request(first, "warning")
    lat.set_path_cancel_request(second, "info")

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
    assert lat.path_cancel_request.error.origin == "operator_cancelled"


def test_a_teardown_with_no_live_path_reports_only_itself(latches):
    lat, notifier = latches
    lat.set_power_off("power off", has_active_path=False)

    assert notifier.operations() == ["set_power_off"]
    assert lat.path_cancel_request is None


# ---------------------------------------------------------------------------
# Latch commits
#
# design_new.md "Latch Table" (No shared consume accessor) + "Latch consumption
# events": the aggregate does not consume. Each field is cleared by its own
# named Phase-3 consumer step, and the commit event comes from that step's own
# consume-side call — `PathDispatcher.cancel_path(...)`,
# `RouteExecutor.remove_train_route_run(...)`, or
# `TrainController.complete_power_off()`. Those emitters are covered where they
# live; here we only pin that clearing is silent and side-effect-free.


def test_clearing_a_latch_emits_nothing_from_the_aggregate(latches):
    lat, notifier = latches
    lat.set_route_deactivate("route deactivated", has_active_path=False)
    notifier.events.clear()

    lat.clear("route_deactivate")
    assert lat.route_deactivate is False
    assert notifier.events == []


def test_the_committed_payload_survives_until_the_step_reads_it(latches):
    """The consumer step reads the payload, then clears: the `kind` and the
    `ErrorRecord` it forwards into `cancel_path(...)` must both still be
    there when it does."""
    lat, _ = latches
    err = ErrorRecord("operator cancelled", "operator_cancelled")
    lat.set_path_cancel_request(err, "info")

    payload = lat.path_cancel_request
    lat.clear("path_cancel_request")
    assert payload.kind == "info"
    assert payload.error is err
    assert lat.path_cancel_request is None


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
        ("deviation", "info"),
        ("no_authority", "info"),
        ("caution", "debug"),
        ("authority_stop", "debug"),
        ("speed_limit", "debug"),
    ],
)
def test_holding_codes_are_info_and_the_rest_debug(code, expected):
    """The two codes that hold the train at zero go out as `info` — an operator
    wants them in the stream — and the rest as `debug` (design_new.md "A changed
    reason code is an event", Kind selection). Neither is a `warning`, so this
    event never colours the stream as a fault; the accompanying
    `test_an_info_reason_code_change_never_surfaces_as_an_error` proves it stays
    out of `latest_error_for`."""
    h = ReasonCodeHarness()
    h.emit_for(code)
    assert h.notifications.events[0]["kind"] == expected


def test_an_info_reason_code_change_never_surfaces_as_an_error():
    """design_new.md "A changed reason code is an event" (No `ErrorRecord`):
    keeping the reason-code event below `warning`, and carrying no `ErrorRecord`,
    keeps it out of `NotificationCenter.latest_error_for(...)`, so the
    no-authority banner still answers with a real failure rather than this
    routine limit event. Even emitted last, the `info` reason-code event must not
    mask or replace the train's live failure."""
    from notification_center import Kind, NotificationCenter, train_ref
    from runtime_payloads import ErrorRecord

    center = NotificationCenter()
    tid = "TRAIN_A"

    # A genuine, still-active failure for the train (surfaceable).
    failure = center.emit(
        source="RouteExecutor",
        operation="plan_task",
        kind=Kind.WARNING,
        message="no path to target",
        entities=[train_ref(tid)],
        error=ErrorRecord("no path to target", "no_route_found"),
        condition_key=f"{tid}:blocked",
    )

    # The reason-code change, shaped exactly as `_report_atc_reason_change`
    # emits it: `info`, no `ErrorRecord`, no condition key — and emitted after
    # the failure, so it is the newest event for the train.
    center.emit(
        source="TrainController",
        operation="tick",
        kind=Kind.INFO,
        message="ATC limit reason none -> no_authority",
        entities=[train_ref(tid)],
        payload={"reason_code": "no_authority"},
    )

    surfaced = center.latest_error_for("train", tid)
    assert surfaced is failure
    assert surfaced.kind == "warning"


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
