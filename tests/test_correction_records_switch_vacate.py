"""A detector correction that carries the tail off a switch records the train
leaving it (design_new.md "Block occupancy detection", A vacated block moves the
span end to the seam; The correction walks through the train's own blocks only).

A switch is released only when the train's movement report lists its tile as
tail-vacated. A detector correction moves the span without a movement report of
its own, so a switch tile it carries the tail off has to be listed by the next
report, or `Switch.route_owners` names the train on the points for good and the
switch can never be thrown again.

Both corrections can do it. A vacate moves the tail to the seam with the next
block, and a 0 cm switch tile — the default length — has its far seam on that
same point, so the tail lands past it. An entry correction slides the whole span
forward, and the tail can cross a switch behind the head on the way.
"""

from block_occupancy import OccupancyInput
from enums import Direction
from network import RailwayNetwork
from path_dispatcher import PathDispatcher
from simulation import SimulationController
from switch import Switch
from track import TRACK_EW, TRACK_NW, Tile
from train import Train, TrainCar

from tests.segment_test_helpers import make_segment_block_with_tracks


class _Detector(OccupancyInput):
    def __init__(self, detected, live):
        self.detected, self.live = set(detected), set(live)

    def detected_block_ids(self):
        return set(self.detected)

    def read_block_currents(self):
        return {block_id: block_id in self.live for block_id in self.detected}


def _build():
    """[BLK_A (0,0)(1,0)]--[SW_X (2,0), 0 cm]--[BLK_C (3,0)(4,0)]--[BLK_D (5,0)(6,0)]

    Every tile is 10 cm; the switch takes `Switch`'s default length, 0 cm.
    """
    network = RailwayNetwork()
    for x in (0, 1):
        network.add_track(Tile(x, 0, track_bits=TRACK_EW))
    make_segment_block_with_tracks(
        network, "BLK_A", positions=[(0, 0), (1, 0)], name="A", length_cm=20.0
    )
    switch_track = Tile(2, 0, track_bits=TRACK_EW | TRACK_NW)
    network.add_track(switch_track)
    switch = Switch(switch_id="SW_X", x=2, y=0)
    network.add_switch(switch)
    switch_track.switch_id = switch.switch_id
    for x in (3, 4, 5, 6):
        network.add_track(Tile(x, 0, track_bits=TRACK_EW))
    make_segment_block_with_tracks(
        network, "BLK_C", positions=[(3, 0), (4, 0)], name="C", length_cm=20.0
    )
    make_segment_block_with_tracks(
        network, "BLK_D", positions=[(5, 0), (6, 0)], name="D", length_cm=20.0
    )
    return network, switch


def _place_across_switch():
    """A powered 10 cm train standing across SW_X: head 2 cm into BLK_C, tail
    2 cm into BLK_A's last tile. Placement records it on the switch."""
    network, switch = _build()
    sim = SimulationController(network, PathDispatcher(network))
    train = Train(
        train_id="T",
        consist=[TrainCar(car_id="c1", car_type="LOCOMOTIVE", length_cm=10.0)],
        max_speed=60.0,
    )
    network.add_train(train)
    controller = sim._get_or_create_controller(train)
    controller.physics_sync.current_span = network.place_train(
        train, 3, 0, Direction.EAST, head_offset_cm=2.0
    )
    sim._get_or_create_physics(train)
    assert controller.commands.power_on_cmd().success
    span = controller.physics_sync.current_span
    assert (2, 0) in {tuple(t) for t in span.occupied_tiles}
    assert "T" in switch.route_owners.values()
    detector = sim.block_occupancy_detector
    detector.input_interface = _Detector(
        detected={"BLK_A", "BLK_C", "BLK_D"}, live={"BLK_A", "BLK_C"}
    )
    detector.seed_occupied({"BLK_A", "BLK_C"})
    return sim, controller, switch, detector


def _covers_switch(controller) -> bool:
    span = controller.physics_sync.current_span
    return (2, 0) in {tuple(t) for t in span.occupied_tiles}


def test_a_vacate_that_carries_the_tail_past_a_0cm_switch_releases_it():
    sim, controller, switch, detector = _place_across_switch()

    detector.input_interface.live = {"BLK_C"}
    for _ in range(detector.off_threshold):
        sim._detect_block_occupancy()

    assert controller.physics_sync.pending_detected_vacated_blocks == {"BLK_A"}
    # The tail is on BLK_A's seam, which on a 0 cm switch is also BLK_C's.
    assert not _covers_switch(controller)

    sim.update(0.1)

    assert "T" not in switch.route_owners.values()


def test_an_entry_correction_that_carries_the_tail_off_a_switch_releases_it():
    sim, controller, switch, detector = _place_across_switch()

    # The locomotive is already in BLK_D: the head is pulled 18 cm forward to
    # BLK_D's entry seam, and the tail 18 cm with it, off the switch.
    detector.input_interface.live = {"BLK_A", "BLK_C", "BLK_D"}
    for _ in range(detector.on_threshold):
        sim._detect_block_occupancy()

    span = controller.physics_sync.current_span
    assert "BLK_D" in span.occupied_blocks
    assert not _covers_switch(controller)

    sim.update(0.1)

    assert "T" not in switch.route_owners.values()


def test_a_switch_the_span_still_covers_stays_recorded():
    sim, controller, switch, detector = _place_across_switch()

    sim.update(0.1)

    assert _covers_switch(controller)
    assert "T" in switch.route_owners.values()
