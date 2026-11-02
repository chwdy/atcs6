"""A switch whose position the app does not know takes the leg the detector
confirms (design_new.md "Block occupancy detection", An unknown switch takes
the leg the detector confirms; "Switch entity", `position_known`).

A hardware session throws no coil when it starts, so the model's
`Switch.position` may not be where the points are. When a train standing at a
switch's toe gets a new entry on a block only one leg leads to, the points
must lie that way: Observe records it before attribution, so the block is
credited to the train instead of minting an unidentified train.

    [BLK_A (0,0)(1,0)]--[SW_X (2,0) toe W, MAIN->E, DIVERGE->N]--[BLK_C (3,0)(4,0)]
                                         |
                                  [BLK_N (2,1)(2,2)]
"""

from block_occupancy import OccupancyInput
from enums import Direction, OperationMode
from network import RailwayNetwork
from path_dispatcher import PathDispatcher
from simulation import SimulationController
from switch import Switch, SwitchState
from track import TRACK_EW, TRACK_NS, TRACK_NW, Tile
from train import Train, TrainCar

from tests.segment_test_helpers import make_segment_block_with_tracks

BLOCKS = {"BLK_A", "BLK_C", "BLK_N"}


class _Detector(OccupancyInput):
    def __init__(self, live):
        self.live = set(live)

    def detected_block_ids(self):
        return set(BLOCKS)

    def read_block_currents(self):
        return {block_id: block_id in self.live for block_id in BLOCKS}


def _build():
    network = RailwayNetwork()
    for x in (0, 1, 3, 4):
        network.add_track(Tile(x, 0, track_bits=TRACK_EW))
    for y in (1, 2):
        network.add_track(Tile(2, y, track_bits=TRACK_NS))
    switch_track = Tile(2, 0, track_bits=TRACK_EW | TRACK_NW)
    network.add_track(switch_track)
    switch = Switch(switch_id="SW_X", x=2, y=0)
    network.add_switch(switch)
    switch_track.switch_id = switch.switch_id
    make_segment_block_with_tracks(network, "BLK_A", positions=[(0, 0), (1, 0)], length_cm=20.0)
    make_segment_block_with_tracks(network, "BLK_C", positions=[(3, 0), (4, 0)], length_cm=20.0)
    make_segment_block_with_tracks(network, "BLK_N", positions=[(2, 1), (2, 2)], length_cm=20.0)
    return network, switch


def _standing_train(block_x: int, facing: Direction, *, known: bool):
    """A powered, stopped 10 cm train standing in the block at `block_x`."""
    network, switch = _build()
    sim = SimulationController(network, PathDispatcher(network))
    train = Train(
        train_id="T",
        consist=[TrainCar(car_id="c1", car_type="LOCOMOTIVE", length_cm=10.0)],
        max_speed=60.0,
    )
    network.add_train(train)
    controller = sim._get_or_create_controller(train)
    controller.physics_sync.current_span = network.place_train(train, block_x, 0, facing)
    sim._get_or_create_physics(train)
    assert controller.commands.power_on_cmd().success
    home = network.get_track(block_x, 0).block_id
    detector = sim.block_occupancy_detector
    detector.input_interface = _Detector(live={home})
    detector.seed_occupied({home})
    switch.position_known = known
    return sim, switch, detector, home


def _read(sim, live, reads=2):
    sim.block_occupancy_detector.input_interface.live = set(live)
    for _ in range(reads):
        sim._detect_block_occupancy()


def _unidentified(sim):
    return [t for t in sim.network.trains.values() if t.is_unidentified]


def _learned_events(sim):
    return [
        n for n in sim.notification_center.peek_recent(200)
        if n.operation == "record_detected_alignment"
    ]


def test_a_new_entry_down_one_leg_teaches_an_unknown_switch():
    sim, switch, _, home = _standing_train(1, Direction.EAST, known=False)
    assert switch.position is SwitchState.MAIN

    _read(sim, {home, "BLK_N"})

    assert switch.position is SwitchState.DIVERGE
    assert switch.position_known is True
    # Attribution credits the entry to T; the commit follows in the same tick.
    assert sim.controllers["T"].physics_sync.pending_detected_entered_blocks == {"BLK_N"}
    assert not _unidentified(sim)
    (event,) = _learned_events(sim)
    assert event.payload["block_id"] == "BLK_N"
    # The app moved nothing, so no flip is counted.
    assert switch.flip_count == 0


def test_a_known_switch_is_not_relearned():
    """With the position known, the points are trusted: current on the other
    leg is not this train, as before."""
    sim, switch, _, home = _standing_train(1, Direction.EAST, known=True)

    _read(sim, {home, "BLK_N"})

    assert switch.position is SwitchState.MAIN
    assert "BLK_N" not in sim.controllers["T"].physics_sync.pending_detected_entered_blocks
    assert _unidentified(sim)
    assert not _learned_events(sim)


def test_both_legs_lit_leave_the_switch_unknown():
    sim, switch, _, home = _standing_train(1, Direction.EAST, known=False)

    _read(sim, {home, "BLK_C", "BLK_N"})

    assert switch.position_known is False
    assert switch.position is SwitchState.MAIN
    assert not _learned_events(sim)


def test_a_switch_met_from_a_leg_says_nothing():
    """A train on the MAIN leg reaches the toe whatever the points do, so a
    block beyond the other leg cannot be reached through it."""
    sim, switch, _, home = _standing_train(4, Direction.WEST, known=False)
    assert home == "BLK_C"

    _read(sim, {home, "BLK_N"})

    assert switch.position_known is False
    assert not _learned_events(sim)


def test_every_position_write_makes_a_switch_known():
    _, switch = _build()
    switch.position_known = False
    assert switch.apply_position_change(1)[0]
    assert switch.position_known is True and switch.flip_count == 1

    switch.position_known = False
    switch.record_detected_position(0)
    assert switch.position is SwitchState.MAIN
    assert switch.position_known is True
    assert switch.flip_count == 1


def test_activation_sets_whether_switches_are_known():
    network, switch = _build()
    sim = SimulationController(network, PathDispatcher(network))

    sim.activate_interfaces(OperationMode.VIRTUAL_HARDWARE, None, None, None)
    assert switch.position_known is False

    sim.activate_interfaces(OperationMode.SIMULATION, None, None, None)
    assert switch.position_known is True
