"""Occupancy bench — the device-owning diagnostic session behind the Occupancy
Bench panel (hardware/occupancy_bench.py), plus the dual-source detector read it
is built on (`OccupancyDriver.read_all_sources`).

Exercised against the in-memory `FakeCh347Device`, so no USB hardware is needed.

Run with:
    /opt/homebrew/anaconda3/envs/general/bin/python -m pytest \
        tests/test_occupancy_bench.py -v
"""

import pytest

from hardware_config import (
    ChipConfig,
    ControllerConfig,
    CurrentBinding,
    HardwareConfig,
    PortBinding,
    TrackBinding,
)
from hardware.fake import FakeCh347Device
from hardware.interfaces import build_real_interfaces
from hardware.occupancy_bench import DEFAULT_DRIVE_PERCENT, OccupancyBenchSession


PWM_ADDR = 0x40
INA_ADDR = 0x40 + 0x00  # the fake keys current chips by their own address below


def _config(reversed_blocks=()):
    """Two blocks, each with rails, a '165 bit, and a current channel — the
    fully-wired shape the bench is meant to report on."""
    return HardwareConfig(
        controller=ControllerConfig(),
        chips=[
            ChipConfig("pwm1", "pca9685", i2c_address=PWM_ADDR),
            ChipConfig("in1", "hc165", spi_select="cs1"),
            ChipConfig("ina1", "ina3221", i2c_address=0x41),
        ],
        block_track={
            "B1": TrackBinding("pwm1", 0, 1, reversed="B1" in reversed_blocks),
            "B2": TrackBinding("pwm1", 6, 7, reversed="B2" in reversed_blocks),
        },
        block_occupancy={
            "B1": PortBinding("in1", 2),
            "B2": PortBinding("in1", 7),
        },
        block_occupancy_current={
            "B1": CurrentBinding("ina1", 0, baseline_ma=0.0, threshold_ma=3.0),
            "B2": CurrentBinding("ina1", 1, baseline_ma=0.0, threshold_ma=3.0),
        },
    )


@pytest.fixture
def bench():
    cfg = _config()
    device = FakeCh347Device(present_addresses={PWM_ADDR, 0x41})
    device.open()
    session = OccupancyBenchSession()
    session.open(cfg, open_device=lambda: device)
    yield session, device, cfg
    session.close()


# ---------------------------------------------------------------------------
# read_all_sources — the dual-source read the panel's readout is built on


def test_read_all_sources_reports_both_detectors_while_driven():
    """The whole reason the bench does not just poll the runtime's read: at bench
    drive the zone is `driven`, so `read_block_currents` never samples the '165 —
    and "the trusted detector is lying" cannot be told apart from "the wrong
    detector is trusted" without both readings side by side."""
    device = FakeCh347Device(present_addresses={PWM_ADDR, 0x41})
    inp, blk, _sw = build_real_interfaces(
        device,
        [
            ChipConfig("pwm1", "pca9685", i2c_address=PWM_ADDR),
            ChipConfig("in1", "hc165", spi_select="cs1"),
            ChipConfig("ina1", "ina3221", i2c_address=0x41),
        ],
        block_track={"B1": ("pwm1", 0, 1)},
        block_occupancy={"B1": ("in1", 2)},
        switch_output={},
        block_occupancy_current={"B1": ("ina1", 0, 0.0, 0.003)},
    )
    blk.set_voltage("B1", 6.0)                 # well above the 4 V current floor

    entry = inp.read_all_sources()["B1"]
    assert entry["zone"] == "driven"
    assert entry["source"] == "current"        # the runtime would believe this one
    # ...but BOTH readings are reported, which is the point.
    assert entry["digital"] is not None
    assert entry["current"] is not None
    assert entry["overall"] is entry["current"]


def test_read_all_sources_marks_an_unbound_detector_none_not_false():
    """"Nothing is wired here" and "the detector says clear" are the two answers
    this panel exists to tell apart, so an unbound side must not read False."""
    device = FakeCh347Device(present_addresses={PWM_ADDR})
    inp, _blk, _sw = build_real_interfaces(
        device,
        [
            ChipConfig("pwm1", "pca9685", i2c_address=PWM_ADDR),
            ChipConfig("in1", "hc165", spi_select="cs1"),
        ],
        block_track={"B1": ("pwm1", 0, 1)},
        block_occupancy={"B1": ("in1", 2)},     # digital only — no current channel
        switch_output={},
    )
    entry = inp.read_all_sources()["B1"]
    assert entry["current"] is None
    assert entry["digital"] is not None
    assert entry["source"] == "digital"


def test_read_all_sources_leaves_the_runtime_read_untouched():
    """A diagnostic read must not move the state the runtime's own read depends
    on — the lighting-zone clear window in particular, which counts consecutive
    both-clear reads and would be reset by an interloper."""
    device = FakeCh347Device(present_addresses={PWM_ADDR, 0x41})
    inp, blk, _sw = build_real_interfaces(
        device,
        [
            ChipConfig("pwm1", "pca9685", i2c_address=PWM_ADDR),
            ChipConfig("in1", "hc165", spi_select="cs1"),
            ChipConfig("ina1", "ina3221", i2c_address=0x41),
        ],
        block_track={"B1": ("pwm1", 0, 1)},
        block_occupancy={"B1": ("in1", 2)},
        switch_output={},
        block_occupancy_current={"B1": ("ina1", 0, 0.0, 0.003)},
    )
    blk.set_voltage("B1", 2.0)                 # the lighting zone
    inp.read_block_currents()
    inp.read_block_currents()
    before_counts = dict(inp._lighting_clear_counts)
    before_reported = dict(inp._last_reported)
    before_detail = dict(inp.last_read_detail())

    inp.read_all_sources()

    assert inp._lighting_clear_counts == before_counts
    assert inp._last_reported == before_reported
    assert inp.last_read_detail() == before_detail


# ---------------------------------------------------------------------------
# The bench session


def test_open_lists_every_block_the_bench_can_speak_for(bench):
    session, _device, _cfg = bench
    assert session.status()["block_ids"] == ["B1", "B2"]
    assert session.is_open() is True


def test_a_detector_only_block_still_gets_a_row():
    """A block bound on one side only is exactly the half-wired case this panel
    exists to find, so it belongs on the list rather than being filtered off it."""
    cfg = _config()
    del cfg.block_track["B2"]                   # detectors but no rails
    device = FakeCh347Device(present_addresses={PWM_ADDR, 0x41})
    session = OccupancyBenchSession()
    session.open(cfg, open_device=lambda: device)
    try:
        assert "B2" in session.status()["block_ids"]
        assert session.read()["blocks"]["B2"]["track_bound"] is False
        assert session.read()["blocks"]["B1"]["track_bound"] is True
    finally:
        session.close()


def test_play_drives_every_block_in_the_main_direction(bench):
    session, device, _cfg = bench
    session.drive(True, percent=DEFAULT_DRIVE_PERCENT, flipped=[])
    # 40% of the 12 V full scale: the forward channel of each pair carries it.
    assert device.duty_cycle(PWM_ADDR, 0) == pytest.approx(0.4, abs=0.01)
    assert device.duty_cycle(PWM_ADDR, 1) == 0.0
    assert device.duty_cycle(PWM_ADDR, 6) == pytest.approx(0.4, abs=0.01)
    assert device.duty_cycle(PWM_ADDR, 7) == 0.0
    assert session.status()["volts"] == pytest.approx(4.8)


def test_a_flipped_block_drives_the_other_rail_and_only_that_block(bench):
    session, device, _cfg = bench
    session.drive(True, percent=DEFAULT_DRIVE_PERCENT, flipped=["B2"])
    assert device.duty_cycle(PWM_ADDR, 0) == pytest.approx(0.4, abs=0.01)  # B1 unchanged
    assert device.duty_cycle(PWM_ADDR, 6) == 0.0
    assert device.duty_cycle(PWM_ADDR, 7) == pytest.approx(0.4, abs=0.01)
    # Never both inputs of one H-bridge at once, whichever way the block runs.
    assert device.duty_cycle(PWM_ADDR, 6) == 0.0


def test_the_bench_flip_rides_on_top_of_the_binding_and_does_not_edit_it():
    """A block already wired `reversed` runs the OTHER rail unflipped, and the
    bench flip inverts that in turn. The binding is a wiring fact the panel must
    never rewrite — the detection rail has to stay where it is soldered."""
    cfg = _config(reversed_blocks={"B2"})
    device = FakeCh347Device(present_addresses={PWM_ADDR, 0x41})
    session = OccupancyBenchSession()
    session.open(
        cfg,
        open_device=lambda: device,
        track_polarity_of=lambda block_id: cfg.block_track[block_id].reversed,
    )
    try:
        session.drive(True, percent=DEFAULT_DRIVE_PERCENT, flipped=[])
        assert device.duty_cycle(PWM_ADDR, 7) == pytest.approx(0.4, abs=0.01)
        assert device.duty_cycle(PWM_ADDR, 6) == 0.0

        session.drive(True, percent=DEFAULT_DRIVE_PERCENT, flipped=["B2"])
        assert device.duty_cycle(PWM_ADDR, 6) == pytest.approx(0.4, abs=0.01)
        assert device.duty_cycle(PWM_ADDR, 7) == 0.0

        assert cfg.block_track["B2"].reversed is True   # untouched
    finally:
        session.close()


def test_flipped_replaces_the_set_rather_than_merging(bench):
    session, device, _cfg = bench
    session.drive(True, percent=DEFAULT_DRIVE_PERCENT, flipped=["B1", "B2"])
    session.drive(True, percent=DEFAULT_DRIVE_PERCENT, flipped=["B1"])
    assert session.status()["flipped"] == ["B1"]
    assert device.duty_cycle(PWM_ADDR, 6) == pytest.approx(0.4, abs=0.01)  # B2 back to main


def test_stop_drops_every_block_to_zero(bench):
    session, device, _cfg = bench
    session.drive(True, percent=DEFAULT_DRIVE_PERCENT, flipped=["B2"])
    session.drive(False)
    for channel in (0, 1, 6, 7):
        assert device.duty_cycle(PWM_ADDR, channel) == 0.0
    assert session.status()["running"] is False


def test_reopening_a_running_bench_re_asserts_the_drive(bench):
    """Reopening builds a fresh TrackDriver and runs `configure()`, which resets
    every PWM channel — a bench that was running used to come back with dead
    rails while still reporting itself as running, and every block silently fell
    back to its idle-zone detector."""
    session, device, cfg = bench
    session.drive(True, percent=DEFAULT_DRIVE_PERCENT, flipped=["B2"])

    session.open(cfg, open_device=lambda: device)

    assert session.status()["running"] is True
    assert device.duty_cycle(PWM_ADDR, 0) == pytest.approx(0.4, abs=0.01)
    assert device.duty_cycle(PWM_ADDR, 7) == pytest.approx(0.4, abs=0.01)


def test_read_reports_the_signed_commanded_voltage(bench):
    session, _device, _cfg = bench
    session.drive(True, percent=DEFAULT_DRIVE_PERCENT, flipped=["B2"])
    blocks = session.read()["blocks"]
    assert blocks["B1"]["commanded_volts"] == pytest.approx(4.8)
    assert blocks["B2"]["commanded_volts"] == pytest.approx(-4.8)
    assert blocks["B2"]["flipped"] is True
    assert blocks["B1"]["flipped"] is False


def test_the_bench_percent_lands_in_the_driven_zone(bench):
    """40% is not an arbitrary default: it must clear the current-sense drive
    floor, or the bench would silently be testing the '165 instead of the
    current channel it was opened to test."""
    session, _device, _cfg = bench
    session.drive(True, percent=DEFAULT_DRIVE_PERCENT, flipped=[])
    assert all(
        entry["zone"] == "driven" for entry in session.read()["blocks"].values()
    )


def test_read_reports_the_measured_current_and_the_threshold_deciding_it(bench):
    session, _device, _cfg = bench
    entry = session.read()["blocks"]["B1"]
    assert entry["current_ma"] is not None
    assert entry["threshold_ma"] == pytest.approx(3.0)
    assert entry["baseline_ma"] == pytest.approx(0.0)


def test_one_threshold_can_be_swept_across_every_channel(bench):
    """The bench is where the number is found; `hardware_config` is where it is
    saved. Sweeping it must move every block's verdict at once."""
    session, device, _cfg = bench
    # A real load on B1's channel: 20 mA. Binding port 0 -> sensor channel 1.
    device.set_ina3221_current(0x41, 1, 0.020)

    session.set_current_threshold(50.0)          # far above the reading
    assert session.read()["blocks"]["B1"]["current"] is False

    session.set_current_threshold(5.0)           # below it
    assert session.read()["blocks"]["B1"]["current"] is True


def test_the_effective_threshold_is_what_gets_reported(bench):
    """A reading judged against the override but reported beside the configured
    number would make the verdict look inconsistent with its own evidence."""
    session, _device, _cfg = bench
    session.set_current_threshold(12.5)
    entry = session.read()["blocks"]["B1"]
    assert entry["threshold_ma"] == pytest.approx(12.5)
    assert entry["configured_threshold_ma"] == pytest.approx(3.0)


def test_clearing_the_override_restores_each_block_s_configured_threshold(bench):
    session, _device, _cfg = bench
    session.set_current_threshold(12.5)
    session.set_current_threshold(None)
    assert session.status()["threshold_ma"] is None
    assert session.read()["blocks"]["B1"]["threshold_ma"] == pytest.approx(3.0)


def test_the_override_never_edits_the_configured_bindings(bench):
    """Held as an override rather than written into `_current`, so a bench
    session cannot leave a driver judging against a number nobody configured."""
    session, _device, cfg = bench
    session.set_current_threshold(99.0)
    assert cfg.block_occupancy_current["B1"].threshold_ma == pytest.approx(3.0)
    assert session._occupancy._current["B1"][3] == pytest.approx(0.003)


def test_a_negative_threshold_is_clamped_not_stored(bench):
    """|reading − baseline| can never fall under a negative, so the block would
    latch occupied with no way out."""
    session, _device, _cfg = bench
    session.set_current_threshold(-5.0)
    assert session.status()["threshold_ma"] == 0.0


def test_the_override_survives_a_reopen(bench):
    """It lives on the driver object `open()` replaces — the same reason the
    drive has to be re-asserted there."""
    session, device, cfg = bench
    session.set_current_threshold(12.5)
    session.open(cfg, open_device=lambda: device)
    assert session.status()["threshold_ma"] == pytest.approx(12.5)
    assert session.read()["blocks"]["B1"]["threshold_ma"] == pytest.approx(12.5)


def test_setting_a_threshold_does_not_disturb_the_rails(bench):
    """It changes what the readings are judged AGAINST, not what the rails are
    doing — sweeping it must not re-command the track underneath the sweep."""
    session, device, _cfg = bench
    session.drive(True, percent=DEFAULT_DRIVE_PERCENT, flipped=["B2"])
    session.set_current_threshold(9.0)
    assert session.status()["running"] is True
    assert session.status()["flipped"] == ["B2"]
    assert device.duty_cycle(PWM_ADDR, 0) == pytest.approx(0.4, abs=0.01)
    assert device.duty_cycle(PWM_ADDR, 7) == pytest.approx(0.4, abs=0.01)


# ---------------------------------------------------------------------------
# Peak current — the running maximum an operator reads off to pick a channel's
# baseline_ma / threshold_ma
#
# The INA quantizes to its own LSB, so a channel set to 19 mA reads back 18.8.
# These assert against the commanded value with a tolerance wider than that
# step: the peak logic is about WHICH reading is kept, and pinning the exact mA
# would be testing the fake's conversion instead.
MA_TOLERANCE = 0.5


def test_the_peak_holds_the_highest_reading_not_the_latest(bench):
    """The whole point: a channel that spiked once and settled must still report
    the spike, because that is the number a threshold has to clear."""
    session, device, _cfg = bench
    device.set_ina3221_current(0x41, 1, 0.004)
    session.read()
    device.set_ina3221_current(0x41, 1, 0.019)
    session.read()
    device.set_ina3221_current(0x41, 1, 0.002)
    entry = session.read()["blocks"]["B1"]

    assert entry["current_ma"] == pytest.approx(2.0, abs=MA_TOLERANCE)
    assert entry["peak_ma"] == pytest.approx(19.0, abs=MA_TOLERANCE)
    assert entry["peak_samples"] == 3


def test_the_peak_is_chosen_by_magnitude_and_reported_signed(bench):
    """A reversed channel reads negative. Comparing raw values would call its
    quietest moment its peak; the sign is kept so the number still matches a
    reading the operator can see on screen."""
    session, device, _cfg = bench
    device.set_ina3221_current(0x41, 1, 0.005)
    session.read()
    device.set_ina3221_current(0x41, 1, -0.017)
    session.read()

    assert session.read()["blocks"]["B1"]["peak_ma"] == pytest.approx(-17.0, abs=MA_TOLERANCE)


def test_the_summary_names_the_worst_channel_on_the_layout(bench):
    session, device, _cfg = bench
    device.set_ina3221_current(0x41, 1, 0.006)      # B1 -> sensor channel 1
    device.set_ina3221_current(0x41, 2, 0.014)      # B2 -> sensor channel 2
    session.read()

    peak = session.read()["peak"]
    assert peak["block_id"] == "B2"
    assert peak["peak_ma"] == pytest.approx(14.0, abs=MA_TOLERANCE)
    assert peak["samples"] == 4                     # 2 channels x 2 reads


def test_a_layout_with_no_current_channels_reports_no_peak():
    """A bound channel always answers with a number, so "no peak" is what a rig
    with nothing to measure looks like — not a rig that measured zero."""
    cfg = _config()
    cfg.block_occupancy_current = {}
    device = FakeCh347Device(present_addresses={PWM_ADDR, 0x41})
    session = OccupancyBenchSession()
    session.open(cfg, open_device=lambda: device)
    try:
        peak = session.read()["peak"]
        assert peak["samples"] == 0
        assert peak["block_id"] is None
        assert peak["peak_ma"] is None
    finally:
        session.close()


def test_peaks_accumulate_with_the_drive_stopped(bench):
    """The zero-drive noise floor is one of the numbers a threshold has to clear,
    and it is only visible with the rails quiet — so a stopped bench must still
    be measuring."""
    session, device, _cfg = bench
    assert session.status()["running"] is False
    device.set_ina3221_current(0x41, 1, 0.003)
    session.read()

    assert session.read()["blocks"]["B1"]["peak_ma"] == pytest.approx(3.0, abs=MA_TOLERANCE)


def test_changing_the_drive_starts_the_peaks_over(bench):
    """Leakage scales with the applied voltage, so a peak measured at one drive
    level says nothing about another — carrying it across would report three
    times the voltage as this level's noise floor."""
    session, device, _cfg = bench
    device.set_ina3221_current(0x41, 1, 0.018)
    session.read()
    assert session.read()["blocks"]["B1"]["peak_ma"] == pytest.approx(18.0, abs=MA_TOLERANCE)

    session.drive(True, percent=DEFAULT_DRIVE_PERCENT, flipped=[])
    device.set_ina3221_current(0x41, 1, 0.002)

    assert session.read()["blocks"]["B1"]["peak_ma"] == pytest.approx(2.0, abs=MA_TOLERANCE)


def test_re_sending_the_same_drive_keeps_the_peaks(bench):
    """A panel that re-asserts its own state must not silently wipe a
    measurement in progress — only an actual change resets."""
    session, device, _cfg = bench
    session.drive(True, percent=DEFAULT_DRIVE_PERCENT, flipped=[])
    device.set_ina3221_current(0x41, 1, 0.016)
    session.read()

    session.drive(True, percent=DEFAULT_DRIVE_PERCENT, flipped=[])
    device.set_ina3221_current(0x41, 1, 0.001)

    assert session.read()["blocks"]["B1"]["peak_ma"] == pytest.approx(16.0, abs=MA_TOLERANCE)


def test_sweeping_the_threshold_keeps_the_peaks(bench):
    """The threshold decides the verdict, never the measurement — a sweep of it
    must not throw away the history being swept against."""
    session, device, _cfg = bench
    device.set_ina3221_current(0x41, 1, 0.012)
    session.read()

    session.set_current_threshold(20.0)

    assert session.read()["blocks"]["B1"]["peak_ma"] == pytest.approx(12.0, abs=MA_TOLERANCE)


def test_reset_peaks_starts_every_channel_over(bench):
    session, device, _cfg = bench
    device.set_ina3221_current(0x41, 1, 0.015)
    device.set_ina3221_current(0x41, 2, 0.011)
    session.read()

    session.reset_peaks()
    payload = session.read()

    assert payload["blocks"]["B1"]["peak_samples"] == 1
    assert payload["blocks"]["B2"]["peak_samples"] == 1
    assert payload["peak"]["samples"] == 2


def test_a_block_with_no_current_binding_reports_no_peak():
    """"Unwired" and "reads 0.0" are the two answers this panel exists to
    separate, and that holds for the peak as much as for the live reading."""
    cfg = _config()
    del cfg.block_occupancy_current["B2"]
    device = FakeCh347Device(present_addresses={PWM_ADDR, 0x41})
    session = OccupancyBenchSession()
    session.open(cfg, open_device=lambda: device)
    try:
        entry = session.read()["blocks"]["B2"]
        assert entry["peak_ma"] is None
        assert entry["peak_samples"] == 0
    finally:
        session.close()


def test_reopening_starts_the_peaks_over(bench):
    """A reopen may have picked up a rebound channel; carrying a number across
    would attribute one channel's peak to another block's binding."""
    session, device, cfg = bench
    device.set_ina3221_current(0x41, 1, 0.013)
    session.read()

    session.open(cfg, open_device=lambda: device)

    assert session.read()["blocks"]["B1"]["peak_samples"] == 1


def test_resetting_peaks_on_a_closed_bench_raises(bench):
    session, _device, _cfg = bench
    session.close()
    with pytest.raises(RuntimeError):
        session.reset_peaks()


def test_close_de_energizes_before_letting_go(bench):
    session, device, _cfg = bench
    session.drive(True, percent=DEFAULT_DRIVE_PERCENT, flipped=[])
    session.close()
    for channel in (0, 1, 6, 7):
        assert device.duty_cycle(PWM_ADDR, channel) == 0.0
    assert session.is_open() is False


def test_driving_a_closed_bench_raises_rather_than_silently_doing_nothing():
    session = OccupancyBenchSession()
    with pytest.raises(RuntimeError):
        session.drive(True)
    with pytest.raises(RuntimeError):
        session.read()


def test_an_abandoned_bench_expires_and_de_energizes(monkeypatch, bench):
    """An operator who walks away must not leave a layout energized. The idle
    timeout is this session's version of the watchdog a control loop gets."""
    session, device, _cfg = bench
    session.drive(True, percent=DEFAULT_DRIVE_PERCENT, flipped=[])

    import hardware.occupancy_bench as bench_module

    later = session._last + bench_module.IDLE_TIMEOUT_S + 1
    monkeypatch.setattr(bench_module.time, "monotonic", lambda: later)

    assert session.is_open() is False
    for channel in (0, 1, 6, 7):
        assert device.duty_cycle(PWM_ADDR, channel) == 0.0
