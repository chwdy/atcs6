"""The occupancy bench's block check (design_new.md "Hardware setup", Occupancy
bench, Block check) and the Virtual Hardware swamp model it relies on
(design_new.md "Hardware setup", Virtual Hardware).

Exercised against the in-memory `FakeCh347Device`, so no USB hardware is needed.

Run with:
    /opt/homebrew/anaconda3/envs/general/bin/python -m pytest tests/test_block_check.py -v
"""

import pytest

from hardware import virtual_rig
from hardware.fake import FakeCh347Device
from hardware.occupancy_bench import (
    BlockCheckRefused,
    OccupancyBenchSession,
    summarize_block_check,
)
from hardware_config import (
    ChipConfig,
    ControllerConfig,
    CurrentBinding,
    HardwareConfig,
    PortBinding,
    TrackBinding,
)

PWM_ADDR = 0x40
INA_ADDR = 0x41


def _config(active_low=False):
    """Two fully wired blocks: rails, a '165 bit, and a current channel each."""
    return HardwareConfig(
        controller=ControllerConfig(),
        chips=[
            ChipConfig("pwm1", "pca9685", i2c_address=PWM_ADDR),
            ChipConfig("in1", "hc165", spi_select="cs1", active_low=active_low),
            ChipConfig("ina1", "ina3221", i2c_address=INA_ADDR),
        ],
        block_track={
            "B1": TrackBinding("pwm1", 0, 1),
            "B2": TrackBinding("pwm1", 6, 7),
        },
        block_occupancy={
            "B1": PortBinding("in1", 2),
            "B2": PortBinding("in1", 7),
        },
        block_occupancy_current={
            "B1": CurrentBinding("ina1", 0, baseline_ma=0.0, threshold_ma=3.0),
            "B2": CurrentBinding("ina1", 1, baseline_ma=0.0, threshold_ma=3.0),
        },
        current_sense_min_drive_v=4.0,
    )


def _open(cfg):
    """A bench on a fake device that models the swamped detector, with every
    block at rest and clear."""
    device = FakeCh347Device(present_addresses={PWM_ADDR, INA_ADDR})
    device.open()
    for block_id in ("B1", "B2"):
        virtual_rig.set_block_presence(cfg, device, block_id, False)
    virtual_rig.install_drive_swamp(cfg, device)
    session = OccupancyBenchSession()
    session.open(cfg, open_device=lambda: device)
    return session, device


def _no_wait(_seconds):
    pass


class _Clock:
    """A clock that moves 0.05 s per read, so a pulse takes a few reads."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        self.now += 0.05
        return self.now


@pytest.fixture
def bench():
    cfg = _config()
    session, device = _open(cfg)
    yield session, device, cfg
    session.close()


def _check(session, block_id):
    return session.check_block(block_id, sleep=_no_wait, clock=_Clock())


# --- the swamp model ------------------------------------------------------


@pytest.mark.parametrize("active_low", [False, True])
def test_a_driven_rail_swamps_its_own_digital_bit_only(active_low):
    cfg = _config(active_low=active_low)
    session, device = _open(cfg)
    try:
        session._track.set_voltage("B1", 4.0)
        sources = session._occupancy.read_all_sources()
        assert sources["B1"]["digital"] is True
        assert sources["B2"]["digital"] is False
        session._track.set_voltage("B1", 0.0)
        assert session._occupancy.read_all_sources()["B1"]["digital"] is False
    finally:
        session.close()


# --- the block check against the fake device ------------------------------


def test_an_empty_well_wired_block_checks_out_with_its_current_channel_unchecked(bench):
    session, device, _cfg = bench
    result = _check(session, "B1")
    assert result["verdict"] == "wired"
    assert result["digital"] == "followed_drive"
    assert result["current"] == "none"
    assert result["others"] == []
    assert result["volts"] == 4.0
    assert result["pulse_reads"] > 1
    # The block is back at 0 V afterwards.
    assert device.duty_cycle(PWM_ADDR, 0) == 0 and device.duty_cycle(PWM_ADDR, 1) == 0


def test_a_block_with_a_load_on_it_reads_present_at_rest_and_checks_out(bench):
    session, device, cfg = bench
    virtual_rig.set_block_presence(cfg, device, "B2", True)
    result = _check(session, "B2")
    assert result["digital"] == "present_at_rest"
    assert result["current"] == "load"
    assert result["verdict"] == "wired"
    assert result["peak_ma"] and result["peak_ma"] > 3.0


def test_a_bit_wired_to_another_blocks_rails_is_reported_as_crossed(bench):
    session, device, _cfg = bench
    # Miswire the fake: B2's rails swamp B1's bit.
    device.set_drive_swamp([(PWM_ADDR, (6, 7), "cs1", 2, False)])
    result = _check(session, "B2")
    assert result["verdict"] == "crossed"
    assert result["others"] == [{"block_id": "B1", "changed": ["digital"]}]


def test_a_bit_that_never_answers_is_reported_as_no_response(bench):
    session, device, _cfg = bench
    device.set_drive_swamp([])          # nothing reaches the '165
    result = _check(session, "B1")
    assert result["verdict"] == "no_response"
    assert result["digital"] == "no_response"


def test_a_running_bench_drive_stops_before_the_check(bench):
    session, device, _cfg = bench
    session.drive(True, percent=40.0)
    result = _check(session, "B1")
    assert result["verdict"] == "wired"
    assert session.status()["running"] is False
    assert device.duty_cycle(PWM_ADDR, 6) == 0      # B2 is at 0 V too


def test_the_block_goes_back_to_zero_when_a_read_fails_mid_pulse(bench, monkeypatch):
    session, device, _cfg = bench
    real_read = session._occupancy.read_all_sources
    calls = {"n": 0}

    def failing_read():
        calls["n"] += 1
        if calls["n"] > 4:          # after the rest reads, inside the pulse
            raise IOError("current channel did not answer")
        return real_read()

    monkeypatch.setattr(session._occupancy, "read_all_sources", failing_read)
    with pytest.raises(IOError):
        _check(session, "B1")
    assert device.duty_cycle(PWM_ADDR, 0) == 0 and device.duty_cycle(PWM_ADDR, 1) == 0


def test_a_block_with_no_rail_binding_is_refused(bench):
    session, _device, _cfg = bench
    with pytest.raises(BlockCheckRefused):
        _check(session, "B9")


def test_a_closed_bench_refuses_the_check():
    session = OccupancyBenchSession()
    with pytest.raises(Exception):
        session.check_block("B1", sleep=_no_wait, clock=_Clock())


# --- the verdict on its own -----------------------------------------------


def _read(**blocks):
    return {bid: {"digital": d, "current": c, "current_ma": ma} for bid, (d, c, ma) in blocks.items()}


def test_summary_present_at_rest_without_a_load_is_not_proof():
    rest = [_read(B1=(True, False, 0.0))]
    pulse = [_read(B1=(True, False, 0.0))]
    result = summarize_block_check("B1", 4.0, rest, pulse)
    assert result["verdict"] == "present_without_load"


def test_summary_counts_a_current_channel_that_wakes_on_another_block_as_crossed():
    rest = [_read(B1=(False, False, 0.0), B2=(False, False, 0.0))]
    pulse = [_read(B1=(True, False, 0.0), B2=(False, True, 9.0))]
    result = summarize_block_check("B1", 4.0, rest, pulse)
    assert result["verdict"] == "crossed"
    assert result["others"] == [{"block_id": "B2", "changed": ["current"]}]


def test_summary_ignores_a_block_already_occupied_at_rest():
    rest = [_read(B1=(False, False, 0.0), B2=(True, True, 9.0))]
    pulse = [_read(B1=(True, False, 0.0), B2=(True, True, 9.0))]
    assert summarize_block_check("B1", 4.0, rest, pulse)["others"] == []


def test_summary_without_a_digital_binding_says_so():
    rest = [{"B1": {"digital": None, "current": False, "current_ma": 0.0}}]
    pulse = [{"B1": {"digital": None, "current": True, "current_ma": 9.0}}]
    result = summarize_block_check("B1", 4.0, rest, pulse)
    assert result["digital"] == "unbound"
    assert result["verdict"] == "unbound"
