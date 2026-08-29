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
from constants import OCCUPANCY_OFF_THRESHOLD, OCCUPANCY_ON_THRESHOLD
from enums import BlockState, OperationMode, TrainState
from network import RailwayNetwork
from path_dispatcher import PathDispatcher
from simulation import SimulationController
from track import Tile, TRACK_EW
from entity_ids import segment_id_for_block
from runtime_payloads import ErrorRecord


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
        track.segment_id = segment_id_for_block(f"BLK_{x}")
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
    sim.unidentified_clock = lambda: sim._fake_clock
    sim._fake_clock = 0.0
    return network, sim, detector


def poll_to_confirm(sim):
    """Poll enough passes for a newly-present current to be confirmed.

    The mirror of the `OCCUPANCY_OFF_THRESHOLD` loops below: confirming takes
    `OCCUPANCY_ON_THRESHOLD` consecutive reads, so a test that wants a block
    held asks for that many rather than assuming one read is enough.
    """
    for _ in range(OCCUPANCY_ON_THRESHOLD):
        sim.poll_hardware_occupancy()


# ----------------------------------------------------------- it does commit


def test_a_stopped_poll_commits_a_detected_occupancy():
    network, sim, detector = build_sim()
    detector.current = {"BLK_1": True}

    poll_to_confirm(sim)                   # update() is never called

    block = network.blocks["BLK_1"]
    assert block.occupied_by is not None
    assert block.state == BlockState.OCCUPIED
    # Nothing the app controls is on the layout, so the occupant is an unidentified train —
    # committed through the ordinary per-train chain, not a side channel.
    assert network.trains[block.occupied_by].is_unidentified is True


def test_a_stopped_poll_vacates_when_the_detector_clears():
    network, sim, detector = build_sim()
    detector.current = {"BLK_1": True}
    poll_to_confirm(sim)
    assert network.blocks["BLK_1"].occupied_by is not None

    # Fail-safe debounce: quick to occupy, slow to clear.
    detector.current = {}
    for _ in range(OCCUPANCY_OFF_THRESHOLD):
        sim.poll_hardware_occupancy()

    assert network.blocks["BLK_1"].occupied_by is None
    assert network.blocks["BLK_1"].state == BlockState.FREE


def test_a_stopped_poll_tracks_an_unidentified_train_moving_between_blocks():
    network, sim, detector = build_sim()
    detector.current = {"BLK_0": True}
    poll_to_confirm(sim)
    unidentified_id = network.blocks["BLK_0"].occupied_by

    # It rolls on: BLK_1 lights up before BLK_0 has cleared its debounce.
    detector.current = {"BLK_0": True, "BLK_1": True}
    sim.poll_hardware_occupancy()
    detector.current = {"BLK_1": True}
    for _ in range(OCCUPANCY_OFF_THRESHOLD):
        sim.poll_hardware_occupancy()

    assert network.blocks["BLK_0"].occupied_by is None
    assert network.blocks["BLK_1"].occupied_by == unidentified_id


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


# ------------------------------------------------ a stopped pass consumes no latch


class _ArrivalSpy:
    """Records whether Phase-3 step 3b reached the dispatcher."""

    def __init__(self):
        self.completion_calls = 0

    def on_controller_progress(self, report):
        pass

    def active_path_id_by_train(self, train_id):
        return "PATH_1"

    def try_complete_active_path(self, train_id):
        self.completion_calls += 1


def _stopped_train_carrying_a_latch():
    """A powered-off train with a detected block change this pass and a
    `path_cancel_request` waiting for Phase 3 to commit."""
    from train import Train

    network, sim, _detector = build_sim()
    train = Train(train_id="T1")
    network.add_train(train)
    controller = sim.get_or_create_controller("T1")
    controller.route_hooks.phase_latches.set_path_cancel_request(
        ErrorRecord("operator cancelled", "operator_cancelled"), "info"
    )
    # Only a train Step 0 attributed a change to reaches Phase 3 while OFF.
    sim._detected_change_train_ids = {"T1"}
    return sim, train, controller


def test_a_stopped_pass_does_not_consume_a_pending_latch():
    """design_new.md "Latch Table": "A stopped session consumes no latch."

    Committing one is authored work, and a stopped session causes nothing. The
    train is stopped, so the gate would otherwise be satisfied on this pass.
    """
    sim, train, controller = _stopped_train_carrying_a_latch()

    sim._commit_train_observe(controller, train, observing_only=True)

    assert controller.route_hooks.phase_latches.path_cancel_request is not None


def test_the_resumed_pass_consumes_the_latch_the_stopped_pass_kept():
    """The latch keeps its payload across the stop and is consumed by the first
    pass of the resumed session whose gate holds — it is deferred, not lost."""
    sim, train, controller = _stopped_train_carrying_a_latch()
    sim._commit_train_observe(controller, train, observing_only=True)

    sim._commit_train_act(controller, train)

    assert controller.route_hooks.phase_latches.path_cancel_request is None


def test_a_stopped_pass_reaches_no_arrival_verdict():
    """Arrival is a statement about live authority, and a stopped session
    exercises none — so step 3b does not run either (design_new.md "Running and
    stopped"; the stopped pass commits the detected-blocks report alone)."""
    from train import Train

    network, sim, _detector = build_sim()
    train = Train(train_id="T1")
    network.add_train(train)
    controller = sim.get_or_create_controller("T1")
    sim._detected_change_train_ids = {"T1"}
    spy = _ArrivalSpy()
    sim.path_dispatcher = spy

    sim._commit_train_observe(controller, train, observing_only=True)

    assert spy.completion_calls == 0


# ------------------------------------------- the occupied-seconds counter runs


def test_a_stopped_poll_accrues_occupied_seconds():
    """design_new.md "Maintenance counters": `Block.occupied_seconds` is an
    observing measurement — a locomotive standing on a block wears that block
    whether or not the session is running — so a stopped pass accrues the wall
    seconds since the previous pass."""
    network, sim, detector = build_sim()
    detector.current = {"BLK_1": True}
    block = network.blocks["BLK_1"]

    poll_to_confirm(sim)
    # Only now is the block held, so the only interval behind the counter is the
    # gap between the debounce reads themselves — milliseconds, not a real stand.
    assert block.occupied_by is not None
    assert block.occupied_seconds < 1.0

    sim._last_pass_wall_time -= 2.0        # stand there for two seconds
    sim.poll_hardware_occupancy()

    assert block.occupied_seconds >= 2.0


def test_a_stopped_poll_accrues_nothing_for_an_empty_block():
    network, sim, detector = build_sim()
    detector.current = {"BLK_1": True}
    sim.poll_hardware_occupancy()

    sim._last_pass_wall_time -= 2.0
    sim.poll_hardware_occupancy()

    assert network.blocks["BLK_0"].occupied_seconds == 0.0


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
