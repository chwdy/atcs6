"""The detectors keep being read while the simulation is stopped.

design_new.md "Power -> Block occupancy detection": in real mode occupancy is
whatever the hardware says it is. Pressing Stop stops the app driving trains; it
does not stop a loco sitting on the rails from being detected. Before
`poll_hardware_occupancy` existed, Step 0 only ever ran from inside `update()`,
which the runtime loop skips while stopped — so a live rig with a train standing
on it read FREE on every block, including at boot, which is stopped by
definition.

These cover the poll itself (it commits, and it vacates), what it must NOT do
(no planning, no command drain, no motion, no clock advance), and that sim mode
opts out.

Run with:
    /opt/homebrew/anaconda3/envs/general/bin/python -m pytest \
        tests/test_stopped_hardware_poll.py -v
"""

from block import Block
from block_occupancy import OccupancyInput
from constants import OCCUPANCY_OFF_THRESHOLD
from enums import BlockState, OperationMode, TrainState
from network import RailwayNetwork
from path_dispatcher import PathDispatcher
from simulation import SimulationController
from track import Tile, TRACK_EW


class FakeDetector(OccupancyInput):
    """A hand-driven per-block current map, standing in for real detectors."""

    def __init__(self):
        self.current = {}
        self.reads = 0

    def read_block_currents(self):
        self.reads += 1
        return dict(self.current)


def build_sim(mode=OperationMode.REAL, num_blocks: int = 3):
    """A straight E-W line, one block per tile (BLK_0 .. BLK_{n-1})."""
    network = RailwayNetwork()
    for x in range(num_blocks):
        track = Tile(x=x, y=0, track_bits=TRACK_EW)
        track.segment_id = f"BLK_{x}"
        network.add_track(track)
        block = Block(block_id=f"BLK_{x}", length_cm=100.0)
        block.ordered_tiles = [(x, 0, "W")]
        network.blocks[f"BLK_{x}"] = block
    detector = FakeDetector()
    sim = SimulationController(
        network,
        PathDispatcher(network),
        operation_mode=mode,
        input_interface=detector,
    )
    sim.ghost_clock = lambda: sim._fake_clock
    sim._fake_clock = 0.0
    return network, sim, detector


# ----------------------------------------------------------- it does commit


def test_a_stopped_poll_commits_a_detected_occupancy():
    network, sim, detector = build_sim()
    detector.current = {"BLK_1": True}

    sim.poll_hardware_occupancy()          # update() is never called

    block = network.blocks["BLK_1"]
    assert block.occupied_by is not None
    assert block.state == BlockState.OCCUPIED
    # Nothing the app controls is on the layout, so the occupant is a ghost —
    # committed through the ordinary per-train chain, not a side channel.
    assert network.trains[block.occupied_by].is_ghost is True


def test_a_stopped_poll_vacates_when_the_detector_clears():
    network, sim, detector = build_sim()
    detector.current = {"BLK_1": True}
    sim.poll_hardware_occupancy()
    assert network.blocks["BLK_1"].occupied_by is not None

    # Fail-safe debounce: quick to occupy, slow to clear.
    detector.current = {}
    for _ in range(OCCUPANCY_OFF_THRESHOLD):
        sim.poll_hardware_occupancy()

    assert network.blocks["BLK_1"].occupied_by is None
    assert network.blocks["BLK_1"].state == BlockState.FREE


def test_a_stopped_poll_tracks_a_ghost_moving_between_blocks():
    network, sim, detector = build_sim()
    detector.current = {"BLK_0": True}
    sim.poll_hardware_occupancy()
    ghost_id = network.blocks["BLK_0"].occupied_by

    # It rolls on: BLK_1 lights up before BLK_0 has cleared its debounce.
    detector.current = {"BLK_0": True, "BLK_1": True}
    sim.poll_hardware_occupancy()
    detector.current = {"BLK_1": True}
    for _ in range(OCCUPANCY_OFF_THRESHOLD):
        sim.poll_hardware_occupancy()

    assert network.blocks["BLK_0"].occupied_by is None
    assert network.blocks["BLK_1"].occupied_by == ghost_id


# --------------------------------------------------- stopped stays stopped


def test_a_stopped_poll_does_not_advance_the_simulation_clock():
    _network, sim, detector = build_sim()
    detector.current = {"BLK_1": True}

    sim.poll_hardware_occupancy()

    assert sim.simulation_time == 0.0


def test_a_stopped_poll_does_not_drain_the_command_inbox():
    """Stop has to keep meaning stop: the poll runs Step 0 and its commit, not
    Phase 2, so an inbox command still waits for the resume that drains it."""
    from train import Train

    network, sim, detector = build_sim()
    network.add_train(Train(train_id="T1"))
    controller = sim.get_or_create_controller("T1")
    controller.commands.enqueue_command("power_on")
    assert controller.ops.state == TrainState.OFF

    detector.current = {"BLK_1": True}
    sim.poll_hardware_occupancy()

    assert len(controller.commands.command_inbox) == 1
    assert controller.ops.state == TrainState.OFF

    # ... and the resume drains it, so nothing was lost either.
    sim.update(0.1)
    assert controller.commands.command_inbox == []
    assert controller.ops.state != TrainState.OFF


def test_a_stopped_poll_does_not_move_a_train():
    from train import Train

    network, sim, detector = build_sim()
    train = Train(train_id="T1")
    network.add_train(train)
    physics = sim.get_or_create_physics("T1")
    ticks_before = physics._tick_count

    detector.current = {"BLK_1": True}
    sim.poll_hardware_occupancy()

    assert physics._tick_count == ticks_before


# ------------------------------------------------------------ sim mode opts out


def test_sim_mode_does_not_poll():
    """The simulated detector derives current from the live spans, so a poll
    between two motionless passes can only re-report what is already committed.
    It is skipped rather than run for a guaranteed no-op on every idle pass."""
    network, sim, detector = build_sim(mode=OperationMode.SIM)
    detector.current = {"BLK_1": True}

    sim.poll_hardware_occupancy()

    assert detector.reads == 0
    assert network.blocks["BLK_1"].occupied_by is None


def test_a_halted_runtime_does_not_poll():
    network, sim, detector = build_sim()
    sim.failed_runtime = True
    detector.current = {"BLK_1": True}

    sim.poll_hardware_occupancy()

    assert detector.reads == 0
    assert network.blocks["BLK_1"].occupied_by is None
