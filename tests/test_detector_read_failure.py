"""A failed detector transaction is a hardware I/O failure.

design_v2.md "Safe state, watchdog and hardware failure" (Hardware I/O failure
is fatal to the session) and "Occupancy source and drive zones" (Both occupancy
sources are mandatory): a failed detector transaction escalates to
`SimulationController.enter_hardware_failure(...)`, and a missing or unreadable
source is never treated as a clear block.
"""

import pytest

from block import Block
from block_occupancy import OccupancyInput
from entity_ids import segment_id_for_block
from hardware.fake import FakeCh347Device
from hardware.interfaces import build_real_interfaces
from hardware_config import ChipConfig
from network import RailwayNetwork
from simulation import SimulationController
from track import Tile, TRACK_EW


class _RaisingInput(OccupancyInput):
    """A hardware-backed occupancy source whose bus transaction fails."""

    def detected_block_ids(self):
        return {"BLK_0"}

    def read_block_currents(self):
        raise OSError("detector bus timeout")


def _line(num_tiles: int = 2) -> RailwayNetwork:
    network = RailwayNetwork()
    for x in range(num_tiles):
        track = Tile(x=x, y=0, track_bits=TRACK_EW)
        track.segment_id = segment_id_for_block(f"BLK_{x}")
        network.add_track(track)
        network.blocks[f"BLK_{x}"] = Block(
            block_id=f"BLK_{x}", length_cm=100.0, speed_limit_kmh=80.0
        )
    return network


def test_failed_current_read_raises_instead_of_reading_clear():
    """A raising current-sense transaction is a hardware I/O failure, not an
    empty block: `read_block_currents()` propagates the error rather than
    returning the block clear (design_v2.md "Safe state, watchdog and hardware
    failure", Hardware I/O failure is fatal to the session)."""
    dev = FakeCh347Device(present_addresses={0x40, 0x41})
    specs = [
        ChipConfig("pwm1", "pca9685", i2c_address=0x40),
        ChipConfig("in1", "hc165", spi_select="cs1"),
        ChipConfig("cur1", "ina3221", i2c_address=0x41),
    ]
    inp, blk, _sw = build_real_interfaces(
        dev,
        specs,
        block_track={"B1": ("pwm1", 0, 1)},
        block_occupancy={"B1": ("in1", 3)},
        switch_output={},
        block_occupancy_current={"B1": ("cur1", 0, 0.0, 0.015, 0.015)},
        idle_deadband_v=0.25,
    )
    # Drive the block so the current channel is the deciding source.
    blk.set_voltage("B1", 8.0)

    def boom(_channel):
        raise OSError("INA bus timeout")

    inp._current_chips["cur1"].read_current = boom

    with pytest.raises(OSError):
        inp.read_block_currents()


def test_failed_detector_read_latches_hardware_failure():
    """A failed detector read in the runtime step escalates to
    `enter_hardware_failure` and halts the session, rather than propagating out
    of the step (design_v2.md "Detector reading log", A failed read gets no
    line; "Safe state, watchdog and hardware failure")."""
    network = _line()
    sim = SimulationController(network, input_interface=_RaisingInput())
    sim.environment_active = True
    assert sim.occupancy_is_real

    # Must not raise: the read failure is caught and escalated.
    sim.run_train_step(live=True)

    assert sim.hardware_failure is True
    assert sim.runtime_halted is True
