"""Closing a Setup tool zeroes the outputs of the chips that tool drives itself
— not through `OutputSafety`, and without advancing `safe_state_generation` —
and a failed write is a hardware I/O failure (design_v2.md "Setup tools",
Closing a Setup tool restores the safe state and A failed closing write is a
hardware I/O failure).

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


def test_a_timed_out_commission_close_keeps_its_failures_for_reporting():
    """A timeout closes the session inside whichever call noticed it; the failed
    zeroing waits for `take_close_failures()` instead of only being logged."""
    session = CommissionSession()
    session._device = object()
    session._borrowed = True
    session._chips = {"bad": _Chip(fails=True)}
    session._last = 0.0
    session._timeout_s = 0.0

    assert session.status()["open"] is False          # the timeout closed it

    assert session.take_close_failures() == ["bad: bus error"]
    assert session.take_close_failures() == []        # each failure once


def test_a_timed_out_bench_close_hands_its_failures_to_the_next_close():
    session = OccupancyBenchSession()
    session._device = object()
    session._borrowed = True
    session._track = TrackDriver({"bad": _Chip(fails=True)}, {}, safety=_NoSafety())
    session._last = -1e9

    assert session.is_open() is False                 # the timeout closed it

    assert session.close() == ["bad: bus error"]
    assert session.close() == []


# --- the self-test sweep and port pulse --------------------------------------

from hardware import self_test
from hardware.chips.pca9685 import PCA9685
from hardware.fake import FakeCh347Device
from hardware_config import ChipConfig, ControllerConfig, HardwareConfig, TrackBinding


def _pwm_cfg():
    return HardwareConfig(
        controller=ControllerConfig(kind="ch347"),
        chips=[ChipConfig("pwm1", "pca9685", i2c_address=0x40)],
        block_track={"B1": TrackBinding("pwm1", 4, 9)},
    )


def _with_channel_left_on(dev, channel):
    chip = PCA9685(dev, address=0x40)
    chip.configure()
    chip.set_duty_cycle(channel, 0.5)
    assert dev.duty_cycle(0x40, channel) > 0


def test_track_sweep_zeroes_every_channel_of_its_chip(monkeypatch):
    monkeypatch.setattr(self_test, "TRACK_TEST_HOLD_S", 0.0)
    dev = FakeCh347Device(present_addresses={0x40})
    _with_channel_left_on(dev, 12)                    # not one of the block's two

    res = self_test.run_track_test(_pwm_cfg(), "B1", device=dev)

    assert res["ok"] is True and "zero_failures" not in res
    assert dev.duty_cycle(0x40, 12) == 0.0


def test_port_pulse_zeroes_every_channel_of_its_chip():
    dev = FakeCh347Device(present_addresses={0x40})
    _with_channel_left_on(dev, 12)

    res = self_test.run_port_test(
        _pwm_cfg(), "pwm1", 5, "pulse_output", device=dev, pulse_s=0.0
    )

    assert res["ok"] is True
    assert dev.duty_cycle(0x40, 12) == 0.0


def test_a_failed_sweep_zeroing_is_returned_for_reporting(monkeypatch):
    monkeypatch.setattr(self_test, "TRACK_TEST_HOLD_S", 0.0)

    def broken_all_off(self):
        raise OSError("bus error")

    monkeypatch.setattr(PCA9685, "all_off", broken_all_off)
    dev = FakeCh347Device(present_addresses={0x40})

    res = self_test.run_track_test(_pwm_cfg(), "B1", device=dev)

    assert res["ok"] is False
    assert res["zero_failures"] == ["pwm1: bus error"]
    assert "could not zero every output" in res["error"]


# --- a timeout closes the tool on time ----------------------------------------

import threading
import time
from types import SimpleNamespace

import runtime
from hardware import setup_tools


def _open_commission(chip, *, idle_for):
    session = CommissionSession()
    session._device = object()
    session._borrowed = True
    session._chips = {"pwm1": chip}
    session._timeout_s = 30.0
    session._last = time.monotonic() - idle_for
    return session


def test_close_if_idle_closes_an_idle_tool_and_returns_its_failures():
    session = _open_commission(_Chip(fails=True), idle_for=1e9)

    assert session.close_if_idle() == ["pwm1: bus error"]
    assert session.is_holding_outputs() is False


def test_close_if_idle_leaves_a_tool_in_use_open():
    chip = _Chip()
    session = _open_commission(chip, idle_for=0)

    assert session.close_if_idle() == []
    assert session.is_holding_outputs() is True
    assert chip.zeroed is False


def test_close_if_idle_never_waits_on_a_busy_tool():
    session = _open_commission(_Chip(), idle_for=1e9)
    held, release = threading.Event(), threading.Event()

    def hold_the_lock():
        with session._lock:
            held.set()
            release.wait(5)

    worker = threading.Thread(target=hold_the_lock)
    worker.start()
    held.wait(5)
    try:
        assert session.close_if_idle() == []      # returned at once, untouched
        assert session.is_holding_outputs() is True
    finally:
        release.set()
        worker.join()


def test_the_runtime_loop_step_reports_a_failed_idle_close(monkeypatch):
    reported = []
    monkeypatch.setattr(setup_tools, "any_setup_tool_open", lambda: True)
    monkeypatch.setattr(setup_tools, "close_idle_setup_tools", lambda: ["pwm1: bus error"])
    monkeypatch.setattr(
        setup_tools,
        "report_close_failures",
        lambda sim, failures: reported.append((sim, list(failures))),
    )
    sim = object()
    state = SimpleNamespace(simulation_lock=threading.Lock(), sim_controller=sim)

    runtime.close_idle_setup_tools(state)

    assert reported == [(sim, ["pwm1: bus error"])]


def test_the_runtime_loop_step_takes_no_lock_with_no_tool_open(monkeypatch):
    monkeypatch.setattr(setup_tools, "any_setup_tool_open", lambda: False)
    lock = threading.Lock()
    lock.acquire()                                  # would deadlock if taken
    state = SimpleNamespace(simulation_lock=lock, sim_controller=None)

    runtime.close_idle_setup_tools(state)
