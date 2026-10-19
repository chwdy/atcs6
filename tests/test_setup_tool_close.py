"""Closing a Setup tool zeroes the outputs of the chips that tool drives itself
— not through `OutputSafety`, and without advancing `safe_state_generation` —
and a failed write is a hardware I/O failure (design_new.md "Power", Hardware
setup, Closing a Setup tool restores the safe state).

Run with: ``/opt/homebrew/anaconda3/envs/general/bin/python -m pytest
tests/test_setup_tool_close.py -q``
"""

from hardware.commission import CommissionSession
from hardware.interfaces import TrackDriver
from hardware.occupancy_bench import OccupancyBenchSession
from hardware.setup_tools import report_close_failures


class _Chip:
    def __init__(self, fails=False):
        self.fails = fails
        self.zeroed = False

    def all_off(self):
        if self.fails:
            raise OSError("bus error")
        self.zeroed = True


class _NoSafety:
    def enter_safe_state(self):
        raise AssertionError("a Setup tool close must not go through OutputSafety")


def test_track_driver_zeroes_every_chip_itself_and_reports_failures():
    bad, good = _Chip(fails=True), _Chip()
    driver = TrackDriver({"bad": bad, "good": good}, {}, safety=_NoSafety())

    errors = driver.zero_every_channel()

    assert good.zeroed is True
    assert errors == ["bad: bus error"]


def test_commission_close_tries_every_chip_and_returns_failures():
    bad, good = _Chip(fails=True), _Chip()
    session = CommissionSession()
    session._device = object()
    session._borrowed = True
    session._chips = {"bad": bad, "good": good}

    errors = session.close()

    assert good.zeroed is True
    assert errors == ["bad: bus error"]
    assert session._device is None


def test_bench_close_zeroes_its_track_chips_without_output_safety():
    good = _Chip()
    session = OccupancyBenchSession()
    session._device = object()
    session._borrowed = True
    session._track = TrackDriver({"good": good}, {}, safety=_NoSafety())

    assert session.close() == []
    assert good.zeroed is True


class _Runtime:
    def __init__(self):
        self.failures = []

    def enter_hardware_failure(self, failure):
        self.failures.append(failure)


def test_a_failed_close_is_reported_as_a_hardware_failure():
    runtime = _Runtime()

    report_close_failures(runtime, ["bad: bus error"])

    assert len(runtime.failures) == 1
    assert runtime.failures[0].component == "setup_tool"
    assert "bad: bus error" in runtime.failures[0].message


def test_a_clean_close_reports_nothing():
    runtime = _Runtime()

    report_close_failures(runtime, [])

    assert runtime.failures == []
