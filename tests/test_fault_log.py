"""The simulation loop's fault collapsing.

The loop must not exit, so a stuck fault fails every tick. At the 10 Hz tick
rate that is ~600 tracebacks a minute for one problem — the line that says when
it started is gone from the scrollback long before anyone reads it. A dropped
CH347 USB link is the fault that prompted this: it fails every occupancy read
until someone restarts the server.
"""

import pytest

from runtime import FaultLog


def _raised(exc: BaseException) -> BaseException:
    """Give `exc` a real traceback, as the loop's `except` always would."""
    try:
        raise exc
    except BaseException as caught:  # noqa: BLE001 - re-raised immediately below
        return caught


def _lines(capsys) -> list:
    return [line for line in capsys.readouterr().out.splitlines() if line]


def test_the_first_fault_prints_in_full(capsys):
    FaultLog("Loop").report(_raised(OSError("read error")))
    captured = capsys.readouterr()
    out = [line for line in captured.out.splitlines() if line]
    assert "[Loop] ERROR: read error" in out
    assert "[Loop] Continuing despite error..." in out
    # The traceback goes to stderr, where `print_exc()` always put it.
    assert "OSError: read error" in captured.err


def test_identical_repeats_are_silent(capsys):
    log = FaultLog("Loop", summary_every=100)
    for _ in range(50):
        log.report(_raised(OSError("read error")))
    # One report, not fifty: the fault is already on the screen.
    assert sum(line.startswith("[Loop] ERROR:") for line in _lines(capsys)) == 1


def test_a_stuck_fault_is_resummarized_periodically(capsys):
    # Silence is ambiguous between "fixed" and "still broken", so a fault that
    # outlives the summary interval says so — without the traceback.
    log = FaultLog("Loop", summary_every=10)
    for _ in range(21):
        log.report(_raised(OSError("read error")))
    summaries = [line for line in _lines(capsys) if "STILL FAILING" in line]
    assert len(summaries) == 2
    assert "11 consecutive ticks" in summaries[0]
    assert "21 consecutive ticks" in summaries[1]


def test_a_different_fault_prints_and_totals_the_previous_run(capsys):
    log = FaultLog("Loop", summary_every=1000)
    for _ in range(5):
        log.report(_raised(OSError("read error")))
    log.report(_raised(ValueError("something else")))
    out = _lines(capsys)
    assert "[Loop] (previous error repeated 4 more times)" in out
    assert "[Loop] ERROR: something else" in out


def test_the_same_message_from_a_different_type_is_a_different_fault(capsys):
    log = FaultLog("Loop")
    log.report(_raised(OSError("gone")))
    log.report(_raised(RuntimeError("gone")))
    assert sum(line.startswith("[Loop] ERROR:") for line in _lines(capsys)) == 2


def test_recovery_is_reported_with_the_run_length(capsys):
    log = FaultLog("Loop", summary_every=1000)
    for _ in range(3):
        log.report(_raised(OSError("read error")))
    log.clear()
    out = _lines(capsys)
    assert "[Loop] recovered after 3 failed tick(s): read error" in out


def test_clearing_a_healthy_loop_says_nothing(capsys):
    log = FaultLog("Loop")
    for _ in range(5):
        log.clear()
    # Every tick of a healthy session calls this; it must stay silent.
    assert _lines(capsys) == []


def test_a_fault_after_a_recovery_prints_in_full_again(capsys):
    log = FaultLog("Loop")
    log.report(_raised(OSError("read error")))
    log.clear()
    capsys.readouterr()
    log.report(_raised(OSError("read error")))
    # The link dropped a second time — a new incident, not a repeat of the old.
    assert "[Loop] ERROR: read error" in _lines(capsys)


@pytest.mark.parametrize("summary_every", [0, -1])
def test_a_nonsense_summary_interval_cannot_divide_by_zero(summary_every, capsys):
    log = FaultLog("Loop", summary_every=summary_every)
    for _ in range(3):
        log.report(_raised(OSError("read error")))
    assert "STILL FAILING" in capsys.readouterr().out
